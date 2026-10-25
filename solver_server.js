"use strict";
// Long-lived solver process. Protocol (JSON lines):
//   stdin : {"id": 7, "cols": [3,3,2]}      -> start analysing this move list (replaces any running job)
//   stdout: {"id": 7, ...result}            -> partial/final results from C4.session (scores, best, exact, final, over, ...)
//           {"ready": true, "books": {...}} -> sent once, after books are loaded
// argv: [booksDir] [ttBits=24] [totalMs=7000]
const fs = require("fs");
const path = require("path");
const readline = require("readline");
const C = require("./solver.js");

const booksDir = process.argv[2] || "books";
const ttBits = +(process.argv[3] || 24);
const totalMs = +(process.argv[4] || 7000);

C.setTT(new ArrayBuffer(8 << ttBits), ttBits);
const out = (o) => process.stdout.write(JSON.stringify(o) + "\n");
const books = {};
function load(name, fn) {
  const f = path.join(booksDir, name);
  if (!fs.existsSync(f)) { books[name] = "missing"; return; }
  try {
    const buf = fs.readFileSync(f);
    if (name.endsWith(".json")) books[name] = fn(JSON.parse(buf.toString("utf8")));
    else books[name] = fn(new Uint8Array(buf.buffer, buf.byteOffset, buf.length));
  } catch (e) { books[name] = "error: " + e.message; }
}
load("book.json", C.loadBook);
load("book_12ply_distances.dat", C.loadDatBook);
load("book_8ply.dat", C.loadDat8Book);
out({ ready: true, books });

let current = 0;
readline.createInterface({ input: process.stdin }).on("line", (line) => {
  let m; try { m = JSON.parse(line); } catch (_) { return; }
  if (!m || !Array.isArray(m.cols)) return;
  const id = m.id; current = id;
  const s = C.session(m.cols, (r) => { if (current === id) out(Object.assign({ id }, r)); }, { totalMs });
  if (!s) { out({ id, error: "invalid position" }); return; }
  const tick = () => {
    if (current !== id) return;
    if (!s.step(performance.now() + 120)) setImmediate(tick);
  };
  tick();
});
