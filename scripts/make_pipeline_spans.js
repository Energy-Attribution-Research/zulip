#!/usr/bin/env node

const crypto = require('crypto');
const fs = require('fs');
const path = require('path');

function usage() {
  console.error(`Usage:
  node scripts/make_pipeline_spans.js record --output <raw.ndjson> --name <span> --start-ms <ms> --end-ms <ms> --exit-code <n> [--test-path <path>] [--command <command>] [--calibration true|false] [--operation <name>]
  node scripts/make_pipeline_spans.js build  --input <raw.ndjson> --output <spans.json> --traceparent <traceparent> --scenario <scenario>`);
}

function parseArgs(argv) {
  const args = {};
  const optionalValueKeys = new Set(['test-path', 'command', 'calibration', 'operation']);

  for (let i = 0; i < argv.length; i += 1) {
    const token = argv[i];

    if (!token.startsWith('--')) {
      throw new Error(`Unexpected argument: ${token}`);
    }

    const key = token.slice(2);
    const value = argv[i + 1];

    if (value === undefined || value.startsWith('--')) {
      if (optionalValueKeys.has(key)) {
        args[key] = '';
        continue;
      }

      throw new Error(`Missing value for --${key}`);
    }

    args[key] = value;
    i += 1;
  }

  return args;
}

function requireArg(args, key) {
  if (!Object.prototype.hasOwnProperty.call(args, key) || args[key] === '') {
    throw new Error(`Missing required argument --${key}`);
  }

  return args[key];
}

function parseBoolean(value, fallback = false) {
  if (value === undefined) return fallback;
  return ['1', 'true', 'yes'].includes(String(value).toLowerCase());
}

function parseNumber(value, fieldName) {
  const parsed = Number(value);
  if (!Number.isFinite(parsed)) {
    throw new Error(`${fieldName} must be a finite number`);
  }
  return parsed;
}

function traceIdFromTraceparent(traceparent) {
  const match = /^00-([0-9a-f]{32})-[0-9a-f]{16}-[0-9a-f]{2}$/i.exec(traceparent.trim());
  if (!match) {
    throw new Error('traceparent must match W3C format: 00-<32hex trace_id>-<16hex span_id>-<2hex flags>');
  }

  return match[1].toLowerCase();
}

function stableSpanId(traceId, scenario, spanName, testPathOrCommand) {
  return crypto
    .createHash('sha256')
    .update(`${traceId}|${scenario}|${spanName}|${testPathOrCommand}`)
    .digest('hex')
    .slice(0, 16);
}

function ensureParentDirectory(filePath) {
  const parent = path.dirname(path.resolve(filePath));
  fs.mkdirSync(parent, { recursive: true });
}

function recordSpan(args) {
  const output = requireArg(args, 'output');
  const name = requireArg(args, 'name');
  const startMs = parseNumber(requireArg(args, 'start-ms'), 'start-ms');
  const endMs = parseNumber(requireArg(args, 'end-ms'), 'end-ms');
  const exitCode = parseNumber(requireArg(args, 'exit-code'), 'exit-code');

  if (endMs < startMs) {
    throw new Error(`end-ms (${endMs}) cannot be before start-ms (${startMs})`);
  }

  const record = {
    name,
    start_time_ms: startMs,
    end_time_ms: endMs,
    exit_code: exitCode,
    test_path: args['test-path'] || '',
    command: args.command || '',
    calibration: parseBoolean(args.calibration),
    operation: args.operation || 'test-group',
  };

  ensureParentDirectory(output);
  fs.appendFileSync(output, `${JSON.stringify(record)}\n`, { encoding: 'utf8' });
}

function readNdjson(input) {
  if (!fs.existsSync(input)) {
    throw new Error(`Input file not found: ${input}`);
  }

  return fs
    .readFileSync(input, 'utf8')
    .split(/\r?\n/)
    .map((line) => line.trim())
    .filter(Boolean)
    .map((line, index) => {
      try {
        return JSON.parse(line);
      } catch (error) {
        throw new Error(`Invalid NDJSON at line ${index + 1}: ${error.message}`);
      }
    });
}

function buildSpans(args) {
  const input = requireArg(args, 'input');
  const output = requireArg(args, 'output');
  const scenario = requireArg(args, 'scenario');
  const traceId = traceIdFromTraceparent(requireArg(args, 'traceparent'));
  const records = readNdjson(input);

  const spans = records.map((record, index) => {
    const name = record.name;
    if (!name) {
      throw new Error(`Record ${index + 1} is missing name`);
    }

    const startMs = parseNumber(record.start_time_ms, `record ${index + 1} start_time_ms`);
    const endMs = parseNumber(record.end_time_ms, `record ${index + 1} end_time_ms`);
    if (endMs < startMs) {
      throw new Error(`Record ${index + 1} end_time_ms cannot be before start_time_ms`);
    }

    const testPath = record.test_path || '';
    const command = record.command || '';
    const testPathOrCommand = testPath || command;

    return {
      span_id: stableSpanId(traceId, scenario, name, testPathOrCommand),
      trace_id: traceId,
      name,
      start_time_ms: startMs,
      end_time_ms: endMs,
      duration_ms: endMs - startMs,
      attributes: {
        'ci.operation': record.operation || 'test-group',
        'ci.test_path': testPath,
        'ci.command': command,
        'ci.exit_code': Number.isFinite(Number(record.exit_code)) ? Number(record.exit_code) : 0,
        'ci.calibration': Boolean(record.calibration),
      },
    };
  });

  ensureParentDirectory(output);
  fs.writeFileSync(output, `${JSON.stringify(spans, null, 2)}\n`, { encoding: 'utf8' });
}

function main() {
  const [mode, ...argv] = process.argv.slice(2);

  if (!mode || !['record', 'build'].includes(mode)) {
    usage();
    process.exit(1);
  }

  const args = parseArgs(argv);
  if (mode === 'record') {
    recordSpan(args);
  } else {
    buildSpans(args);
  }
}

try {
  main();
} catch (error) {
  console.error(`ERROR: ${error.message}`);
  process.exit(1);
}
