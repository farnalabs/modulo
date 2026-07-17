const fs = require('fs');
const path = require('path');

const rekaDir = path.join(__dirname, '..', 'node_modules', 'reka-ui', 'dist');
const dcts = path.join(rekaDir, 'index.d.cts');
const dts = path.join(rekaDir, 'index.d.ts');

if (!fs.existsSync(rekaDir)) { console.log('reka-ui: not installed'); process.exit(0); }
if (fs.existsSync(dts)) { console.log('reka-ui: index.d.ts ok'); process.exit(0); }
if (!fs.existsSync(dcts)) { console.log('reka-ui: no index.d.cts found'); process.exit(0); }
try {
  fs.copyFileSync(dcts, dts);
  console.log('reka-ui: patched index.d.cts -> index.d.ts');
} catch (err) {
  console.warn('reka-ui patch failed:', err.message);
}
