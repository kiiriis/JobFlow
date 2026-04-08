#!/usr/bin/env node
/**
 * Bridge between Content-Length framed stdio (Claude Code) and
 * newline-delimited JSON (Playwright MCP).
 */

const { spawn } = require('child_process');

const child = spawn('node', ['/Users/krish/node_modules/@playwright/mcp/cli.js', ...process.argv.slice(2)], {
  stdio: ['pipe', 'pipe', 'inherit'],
});

child.on('error', (e) => { process.stderr.write(`Bridge spawn error: ${e}\n`); process.exit(1); });
child.on('exit', (code) => process.exit(code ?? 0));

// === Incoming: Content-Length framed stdin → newline JSON to child ===
let inputBuf = '';
process.stdin.setEncoding('utf8');
process.stdin.on('data', (chunk) => {
  inputBuf += chunk;
  while (true) {
    // Try Content-Length framing first
    const headerEnd = inputBuf.indexOf('\r\n\r\n');
    if (headerEnd !== -1) {
      const header = inputBuf.substring(0, headerEnd);
      const match = header.match(/Content-Length:\s*(\d+)/i);
      if (match) {
        const len = parseInt(match[1], 10);
        const bodyStart = headerEnd + 4;
        if (inputBuf.length >= bodyStart + len) {
          const body = inputBuf.substring(bodyStart, bodyStart + len);
          inputBuf = inputBuf.substring(bodyStart + len);
          child.stdin.write(body + '\n');
          continue;
        }
        break; // Need more data
      }
    }
    // Try bare newline-delimited JSON
    const nlIdx = inputBuf.indexOf('\n');
    if (nlIdx !== -1) {
      const line = inputBuf.substring(0, nlIdx).trim();
      inputBuf = inputBuf.substring(nlIdx + 1);
      if (line) child.stdin.write(line + '\n');
      continue;
    }
    break;
  }
});
process.stdin.on('end', () => child.stdin.end());

// === Outgoing: newline JSON from child → Content-Length framed stdout ===
let outputBuf = '';
child.stdout.setEncoding('utf8');
child.stdout.on('data', (chunk) => {
  outputBuf += chunk;
  let nlIdx;
  while ((nlIdx = outputBuf.indexOf('\n')) !== -1) {
    const line = outputBuf.substring(0, nlIdx).trim();
    outputBuf = outputBuf.substring(nlIdx + 1);
    if (line) {
      const bytes = Buffer.byteLength(line, 'utf8');
      process.stdout.write(`Content-Length: ${bytes}\r\n\r\n${line}`);
    }
  }
});
