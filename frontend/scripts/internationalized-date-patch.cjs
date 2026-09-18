const fs = require('node:fs');
const path = require('node:path');

const mjs = path.join(__dirname, '..', 'node_modules', '@internationalized', 'date', 'dist', 'index.mjs');
const js = path.join(__dirname, '..', 'node_modules', '@internationalized', 'date', 'dist', 'index.js');

if (!fs.existsSync(mjs) && fs.existsSync(js)) {
  fs.copyFileSync(js, mjs);
  console.log('@internationalized/date: index.mjs patched from index.js');
}
