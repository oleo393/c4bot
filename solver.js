"use strict";
// Connect 4 solver, 7 wide x 6 high. Columns are 0-indexed (0..6).
//
// Engine: bitboards (49 bits held as a hi/lo int32 pair, bit = col*7 + row),
// threat-aware negamax + alpha-beta, big persistent transposition table,
// weak (win/draw/loss) solving first, then exact scores by null-window search.
// Total time budget per analyse() call defaults to 7000 ms.
(function (root) {
  const W = 7, H = 6, SIZE = 42;
  const ORDER0 = [3, 2, 4, 1, 5, 0, 6];
  let ORD = ORDER0.slice();            // move-order tie-break (helpers use variants)
  const TIMEOUT = { timeout: true };
  const INF = 1e9;
  const now = () => performance.now();

  // ---------------- bitboard constants ----------------
  const colH = new Int32Array(W), colL = new Int32Array(W);
  let botH = 0, botL = 0, brdH = 0, brdL = 0, evenH = 0, evenL = 0, oddH = 0, oddL = 0;
  (function () {
    for (let c = 0; c < W; c++) for (let r = 0; r < H; r++) {
      const b = c * 7 + r, hi = b >= 32, m = 1 << (hi ? b - 32 : b);
      if (hi) { colH[c] |= m; brdH |= m; if (r === 0) botH |= m; if (r & 1) oddH |= m; else evenH |= m; }
      else { colL[c] |= m; brdL |= m; if (r === 0) botL |= m; if (r & 1) oddL |= m; else evenL |= m; }
    }
  })();
  const botLu = botL >>> 0;
  const c3H = colH[3], c3L = colL[3];
  const c24H = colH[2] | colH[4], c24L = colL[2] | colL[4];

  function pop(x) {
    x = x - ((x >>> 1) & 0x55555555);
    x = (x & 0x33333333) + ((x >>> 2) & 0x33333333);
    x = (x + (x >>> 4)) & 0x0f0f0f0f;
    return Math.imul(x, 0x01010101) >>> 24;
  }

  // Cells where `p` would complete four-in-a-row (empty cells only). Result in wH/wL.
  let wH = 0, wL = 0;
  function dirAdd(pH, pL, s) {
    const t = 32 - s, u = s * 2, v = s * 3;
    const l1L = pL << s, l1H = (pH << s) | (pL >>> t);
    const l2L = pL << u, l2H = (pH << u) | (pL >>> (32 - u));
    const l3L = pL << v, l3H = (pH << v) | (pL >>> (32 - v));
    const r1L = (pL >>> s) | (pH << t), r1H = pH >>> s;
    const r2L = (pL >>> u) | (pH << (32 - u)), r2H = pH >>> u;
    const r3L = (pL >>> v) | (pH << (32 - v)), r3H = pH >>> v;
    let a = l1L & l2L, b = l1H & l2H;
    wL |= a & (l3L | r1L); wH |= b & (l3H | r1H);
    a = r1L & r2L; b = r1H & r2H;
    wL |= a & (l1L | r3L); wH |= b & (l1H | r3H);
  }
  function winPos(pH, pL, mH, mL) {
    wL = (pL << 1) & (pL << 2) & (pL << 3);
    wH = ((pH << 1) | (pL >>> 31)) & ((pH << 2) | (pL >>> 30)) & ((pH << 3) | (pL >>> 29));
    dirAdd(pH, pL, 7); dirAdd(pH, pL, 6); dirAdd(pH, pL, 8);
    wL &= brdL & ~mL; wH &= brdH & ~mH;
  }

  // ---------------- transposition table (persists between calls) ----------------
  // 2 int32 per entry: w1 = data, w0 = keyLow ^ data (lock-less xor check, safe on SharedArrayBuffer)
  // data = keyHigh(17) | flag(2)<<17 | (score+32)(6)<<19 | stones(6)<<25. Buckets of 2, deeper entries replaced first.
  let ttBits = 22, tt = new Int32Array(2 << ttBits);
  let SYMN = 10;                       // use mirror-canonical keys up to this many stones
  let nodes = 0, deadline = Infinity, ctl = null, ctlGen = 0;
  function cb(H, L, sh) {              // 7-bit column starting at bit `sh` of the hi/lo pair
    if (sh + 7 <= 32) return (L >>> sh) & 127;
    if (sh >= 32) return (H >>> (sh - 32)) & 127;
    return ((L >>> sh) | (H << (32 - sh))) & 127;
  }

  // ---------------- BitBully "12-ply with distances" book (book_12ply_distances.dat) ----------------
  // File = N records of 5 bytes: int32 big-endian code + int8 value, sorted ascending by the *signed* code.
  // code: bit stream (MSB first) over columns 0..6, bottom-up. Stone = '1' + colour bit (0 = player 1 = side
  // to move at 12 stones, 1 = player 2); column end = '0' (6 of them, none after column 6). 30 bits, low 2 bits unused.
  // value: >0 player 1 wins, <0 loses, 0 draw; |value| = 100 - plies left until the game ends.
  // Only positions with exactly 12 stones, stored for one of the two mirror images.
  const BK_N = 12, BK_NONE = 99;
  let dbk = null;                        // { keys: Int32Array, vals: Int8Array, n }
  function dbLoad(src) {
    const u8 = src instanceof Uint8Array ? src : new Uint8Array(src);
    if (u8.length === 0 || u8.length % 5 !== 0) return 0;
    const n = u8.length / 5, mk = typeof SharedArrayBuffer !== "undefined" ? SharedArrayBuffer : ArrayBuffer;
    const keys = new Int32Array(new mk(4 * n)), vals = new Int8Array(new mk(n));
    for (let i = 0, o = 0; i < n; i++, o += 5) {
      keys[i] = ((u8[o] << 24) | (u8[o + 1] << 16) | (u8[o + 2] << 8) | u8[o + 3]) & -4;
      vals[i] = u8[o + 4];
      if (i > 0 && keys[i] < keys[i - 1]) return 0;      // not sorted as expected -> reject
    }
    dbk = { keys, vals, n };
    return n;
  }
  function dbEnc(pH, pL, mH, mL, rev) {
    let code = 0;
    for (let i = 0; i < 7; i++) {
      const c = rev ? 6 - i : i, pc = cb(pH, pL, 7 * c), h = pop(cb(mH, mL, 7 * c));
      for (let r = 0; r < h; r++) code = (code << 2) | 2 | (((pc >> r) & 1) ^ 1);
      if (i < 6) code <<= 1;
    }
    return code << 2;
  }
  function dbFind(code) {
    const k = dbk.keys; let lo = 0, hi = dbk.n - 1;
    while (lo <= hi) {
      const mid = (lo + hi) >>> 1, v = k[mid];
      if (v < code) lo = mid + 1; else if (v > code) hi = mid - 1; else return mid;
    }
    return -1;
  }
  // exact negamax score (same scale as negamax()) of a 12-stone position, or BK_NONE if not in the book
  function dbScore(pH, pL, mH, mL) {
    const c1 = dbEnc(pH, pL, mH, mL, false);
    let i = dbFind(c1);
    if (i < 0) { const c2 = dbEnc(pH, pL, mH, mL, true); if (c2 !== c1) i = dbFind(c2); }
    if (i < 0) return BK_NONE;
    const v = dbk.vals[i];
    if (v === 0) return 0;
    const s = ((v < 0 ? -v : v) - 68) >> 1;            // plies left d = 100-|v|; score = (32-d)>>1
    return v > 0 ? s : -s;
  }

  // ---------------- BitBully "8-ply" book (book_8ply.dat) ----------------
  // File = N records of 3 bytes: 24-bit big-endian unsigned code, sorted ascending. Same bit stream as the 12-ply
  // book (see dbEnc): 8 stones -> 22 key bits, shifted left by 2. The low 2 bits hold the result for player 1
  // (= side to move at 8 stones): 0 = draw, 1 = loss. A position that is NOT listed is a WIN for the side to move.
  // Only positions with exactly 8 stones, stored for one of the two mirror images. No distances: win/draw/loss only.
  const B8_N = 8;
  let d8 = null;                         // { keys: Int32Array (low 2 bits cleared), vals: Uint8Array, n }
  function d8Load(src) {
    const u8 = src instanceof Uint8Array ? src : new Uint8Array(src);
    if (u8.length === 0 || u8.length % 3 !== 0) return 0;
    const n = u8.length / 3, mk = typeof SharedArrayBuffer !== "undefined" ? SharedArrayBuffer : ArrayBuffer;
    const keys = new Int32Array(new mk(4 * n)), vals = new Uint8Array(new mk(n));
    let prev = -1;
    for (let i = 0, o = 0; i < n; i++, o += 3) {
      const code = (u8[o] << 16) | (u8[o + 1] << 8) | u8[o + 2];
      if (code <= prev || (code & 3) > 1) return 0;     // not sorted / unknown result value -> reject
      prev = code;
      keys[i] = code & ~3; vals[i] = code & 3;
    }
    d8 = { keys, vals, n };
    return n;
  }
  function d8Find(code) {
    const k = d8.keys; let lo = 0, hi = d8.n - 1;
    while (lo <= hi) {
      const mid = (lo + hi) >>> 1, v = k[mid];
      if (v < code) lo = mid + 1; else if (v > code) hi = mid - 1; else return mid;
    }
    return -1;
  }
  // weak result for the side to move at 8 stones: 1 = win, 0 = draw, -1 = loss (a miss means win)
  function d8Score(pH, pL, mH, mL) {
    const c1 = dbEnc(pH, pL, mH, mL, false);
    let i = d8Find(c1);
    if (i < 0) { const c2 = dbEnc(pH, pL, mH, mL, true); if (c2 !== c1) i = d8Find(c2); }
    return i < 0 ? 1 : (d8.vals[i] === 0 ? 0 : -1);
  }

  // ---------------- exact negamax (scores as in the classic solver) ----------------
  const mvH = new Int32Array(48 * 8), mvL = new Int32Array(48 * 8), mvS = new Int32Array(48 * 8);
  function negamax(pH, pL, mH, mL, n, alpha, beta) {
    if ((++nodes & 4095) === 0 && (now() > deadline || (ctl !== null && Atomics.load(ctl, 0) !== ctlGen))) throw TIMEOUT;
    if (n === SIZE) return 0;
    const sL = (mL >>> 0) + botLu;
    const possL = (sL | 0) & brdL;
    const possH = (mH + botH + (sL > 4294967295 ? 1 : 0)) & brdH;
    winPos(pH, pL, mH, mL);
    if ((wH & possH) | (wL & possL)) return (SIZE + 1 - n) >> 1;
    winPos(pH ^ mH, pL ^ mL, mH, mL);
    const oH = wH, oL = wL;
    let nH = possH, nL = possL;
    const fH = oH & possH, fL = oL & possL;
    if (fH | fL) {
      if (pop(fH) + pop(fL) > 1) return -((SIZE - n) >> 1);
      nH = fH; nL = fL;
    }
    nL &= ~((oL >>> 1) | (oH << 31)); nH &= ~(oH >>> 1);   // never play under an opponent threat
    if ((nH | nL) === 0) return -((SIZE - n) >> 1);
    if (n >= SIZE - 2) return 0;
    let v = -((SIZE - 2 - n) >> 1);
    if (alpha < v) { alpha = v; if (alpha >= beta) return alpha; }
    v = (SIZE - 1 - n) >> 1;
    if (beta > v) { beta = v; if (alpha >= beta) return beta; }

    if (n === BK_N && dbk !== null) { const bv = dbScore(pH, pL, mH, mL); if (bv !== BK_NONE) return bv; }
    if (n === B8_N && d8 !== null) {                       // 8-ply book: exact draw, or win/loss as a bound
      const w8 = d8Score(pH, pL, mH, mL);
      if (w8 === 0) return 0;
      if (w8 > 0) { if (alpha < 1) { alpha = 1; if (alpha >= beta) return alpha; } }
      else if (beta > -1) { beta = -1; if (alpha >= beta) return beta; }
    }

    let kL, kH;
    if (n <= SYMN) {
      let k1 = 0, k2 = 0, f1 = 1, f2 = 4398046511104;
      for (let c = 0; c < 7; c++) {
        const v = cb(pH, pL, 7 * c) + cb(mH, mL, 7 * c) + 1;
        k1 += v * f1; k2 += v * f2; f1 *= 128; f2 /= 128;
      }
      const k = k1 <= k2 ? k1 : k2;
      kL = k | 0; kH = Math.floor(k / 4294967296);
    } else {
      const s3 = (pL >>> 0) + (mL >>> 0) + botLu;
      kL = s3 | 0; kH = (pH + mH + botH + ((s3 / 4294967296) | 0)) | 0;
    }
    let h = kL ^ Math.imul(kH, 0x9E3779B1);
    h = Math.imul(h ^ (h >>> 15), 0x85EBCA6B);
    const j0 = ((Math.imul(h ^ (h >>> 13), 0xC2B2AE35) >>> (32 - ttBits)) & ~1) << 1, j1 = j0 + 2;
    const d0 = tt[j0 + 1], d1 = tt[j1 + 1];
    const m0 = ((tt[j0] ^ d0) === kL) && (d0 & 0x1ffff) === kH && (d0 >>> 17 & 3) !== 0;
    const m1 = !m0 && ((tt[j1] ^ d1) === kL) && (d1 & 0x1ffff) === kH && (d1 >>> 17 & 3) !== 0;
    if (m0 || m1) {
      const e = m0 ? d0 : d1, fl = (e >>> 17) & 3, val = ((e >>> 19) & 63) - 32;
      if (fl === 3) return val;
      if (fl === 1) { if (val > alpha) alpha = val; } else if (val < beta) beta = val;
      if (alpha >= beta) return val;
    }

    // order moves: most new threats first, centre-first on ties
    const multi = pop(nH) + pop(nL) > 1, off = n << 3;
    let cnt = 0;
    for (let i = 0; i < W; i++) {
      const c = ORD[i], bH = nH & colH[c], bL = nL & colL[c];
      if ((bH | bL) === 0) continue;
      let sc = 0;
      if (multi) { winPos(pH | bH, pL | bL, mH, mL); sc = pop(wH) + pop(wL); }
      let j = cnt++;
      while (j > 0 && mvS[off + j - 1] < sc) {
        mvH[off + j] = mvH[off + j - 1]; mvL[off + j] = mvL[off + j - 1]; mvS[off + j] = mvS[off + j - 1]; j--;
      }
      mvH[off + j] = bH; mvL[off + j] = bL; mvS[off + j] = sc;
    }
    const a0 = alpha, cpH = pH ^ mH, cpL = pL ^ mL;
    let best = -100;
    for (let k = 0; k < cnt; k++) {
      const s = -negamax(cpH, cpL, mH | mvH[off + k], mL | mvL[off + k], n + 1, -beta, -alpha);
      if (s > best) { best = s; if (s > alpha) { alpha = s; if (alpha >= beta) break; } }
    }
    const dn = kH | ((best <= a0 ? 2 : best >= beta ? 1 : 3) << 17) | ((best + 32) << 19) | (n << 25);
    let j;
    if (m0) j = j0; else if (m1) j = j1;
    else if ((d0 >>> 17 & 3) === 0) j = j0;
    else if ((d1 >>> 17 & 3) === 0) j = j1;
    else j = (d0 >>> 25) >= (d1 >>> 25) ? j0 : j1;
    tt[j + 1] = dn; tt[j] = kL ^ dn;
    return best;
  }

  // ---------------- depth-limited heuristic search ----------------
  function evalPos(pH, pL, mH, mL, n, aH, aL, oH, oL) {
    // aH/aL: my threat cells, oH/oL: opponent threat cells. Parity rule: the first
    // player profits from threats on odd rows (1-indexed), the second on even rows.
    const first = (n & 1) === 0;
    const mgH = first ? evenH : oddH, mgL = first ? evenL : oddL;
    const ogH = first ? oddH : evenH, ogL = first ? oddL : evenL;
    const aG = pop(aH & mgH) + pop(aL & mgL), aT = pop(aH) + pop(aL);
    const oG = pop(oH & ogH) + pop(oL & ogL), oT = pop(oH) + pop(oL);
    let s = 8 * (aG - oG) + 3 * ((aT - aG) - (oT - oG));
    const qH = pH ^ mH, qL = pL ^ mL;
    s += 3 * (pop(pH & c3H) + pop(pL & c3L) - pop(qH & c3H) - pop(qL & c3L));
    s += pop(pH & c24H) + pop(pL & c24L) - pop(qH & c24H) - pop(qL & c24L);
    return s;
  }
  function hneg(d, pH, pL, mH, mL, n, alpha, beta) {
    if ((++nodes & 4095) === 0 && (now() > deadline || (ctl !== null && Atomics.load(ctl, 0) !== ctlGen))) throw TIMEOUT;
    if (n === SIZE) return 0;
    const sL = (mL >>> 0) + botLu;
    const possL = (sL | 0) & brdL;
    const possH = (mH + botH + (sL > 4294967295 ? 1 : 0)) & brdH;
    winPos(pH, pL, mH, mL);
    const aH = wH, aL = wL;
    if ((aH & possH) | (aL & possL)) return 10000 - n;
    winPos(pH ^ mH, pL ^ mL, mH, mL);
    const oH = wH, oL = wL;
    let nH = possH, nL = possL;
    const fH = oH & possH, fL = oL & possL;
    if (fH | fL) {
      if (pop(fH) + pop(fL) > 1) return -(9999 - n);
      nH = fH; nL = fL;
    }
    nL &= ~((oL >>> 1) | (oH << 31)); nH &= ~(oH >>> 1);
    if ((nH | nL) === 0) return -(9999 - n);
    if (n >= SIZE - 2) return 0;
    if (n === BK_N && dbk !== null) {
      const bv = dbScore(pH, pL, mH, mL);
      if (bv !== BK_NONE) { if (bv === 0) return 0; const dd = bv > 0 ? 31 - 2 * bv : 32 + 2 * bv; return bv > 0 ? 9989 - dd : dd - 9989; }
    }
    if (d === 0) return evalPos(pH, pL, mH, mL, n, aH, aL, oH, oL);
    const multi = pop(nH) + pop(nL) > 1, off = n << 3;
    let cnt = 0;
    for (let i = 0; i < W; i++) {
      const c = ORD[i], bH = nH & colH[c], bL = nL & colL[c];
      if ((bH | bL) === 0) continue;
      let sc = 0;
      if (multi) { winPos(pH | bH, pL | bL, mH, mL); sc = pop(wH) + pop(wL); }
      let j = cnt++;
      while (j > 0 && mvS[off + j - 1] < sc) {
        mvH[off + j] = mvH[off + j - 1]; mvL[off + j] = mvL[off + j - 1]; mvS[off + j] = mvS[off + j - 1]; j--;
      }
      mvH[off + j] = bH; mvL[off + j] = bL; mvS[off + j] = sc;
    }
    const cpH = pH ^ mH, cpL = pL ^ mL;
    let best = -Infinity;
    for (let k = 0; k < cnt; k++) {
      const s = -hneg(d - 1, cpH, cpL, mH | mvH[off + k], mL | mvL[off + k], n + 1, -beta, -alpha);
      if (s > best) { best = s; if (s > alpha) { alpha = s; if (alpha >= beta) break; } }
    }
    return best;
  }

  // ---------------- position handling ----------------
  let cs = { pH: 0, pL: 0, mH: 0, mL: 0, n: 0 };   // pH/pL = stones of the side to move
  let stack = [];
  function possible(mH, mL) {
    const sL = (mL >>> 0) + botLu;
    return { H: (mH + botH + (sL > 4294967295 ? 1 : 0)) & brdH, L: (sL | 0) & brdL };
  }
  // 0 = invalid, 1 = played, 2 = move wins (not applied)
  function step(s, c) {
    if (!Number.isInteger(c) || c < 0 || c >= W) return 0;
    const q = possible(s.mH, s.mL), bH = q.H & colH[c], bL = q.L & colL[c];
    if (!(bH | bL)) return 0;
    winPos(s.pH, s.pL, s.mH, s.mL);
    if ((wH & bH) | (wL & bL)) return 2;
    s.pH ^= s.mH; s.pL ^= s.mL; s.mH |= bH; s.mL |= bL; s.n++;
    return 1;
  }
  function setup(cols) {
    cs = { pH: 0, pL: 0, mH: 0, mL: 0, n: 0 }; stack = [];
    for (const c of cols) {
      const r = step(cs, c);
      if (r === 0) return { error: "invalid move list" };
      if (r === 2) return { over: true, win: true };
    }
    if (cs.n === SIZE) return { over: true, win: false };
    return { ok: true };
  }

  // exact value of a position known to lie in [min, max] (null-window binary search)
  function solveRange(pH, pL, mH, mL, n, min, max) {
    while (min < max) {
      let med = min + ((max - min) >> 1);
      if (med <= 0 && Math.trunc(min / 2) < med) med = Math.trunc(min / 2);
      else if (med >= 0 && Math.trunc(max / 2) > med) med = Math.trunc(max / 2);
      const r = negamax(pH, pL, mH, mL, n, med, med + 1);
      if (r <= med) max = r; else min = r;
    }
    return min;
  }

  // ---------------- analysis ----------------
  // session(): resumable analysis. step(untilTime) does a slice of work and returns true when the final
  // result has been posted, so a worker can yield between slices (and drop a stale job in ~100 ms
  // without being terminated, which keeps the transposition table alive between moves).
  function session(cols, post, opts) {
    opts = opts || {};
    const t0 = now();
    const total = opts.totalMs || 7000;                       // hard budget for the whole call
    const hardEnd = t0 + total - (opts.marginMs === undefined ? 40 : opts.marginMs);
    const st = setup(cols);
    if (st.error) { post({ final: true, error: st.error }); return null; }
    if (st.over) { post({ final: true, over: true, draw: !st.win }); return null; }
    const R = cs, n = R.n;

    // Opening book: empty board (first player: centre wins, c/e draw, edges lose)
    if (n === 0) {
      post({ scores: [-2, -1, 0, 1, 0, -1, -2], exact: true, final: true, best: 3, outcome: "win" });
      return null;
    }

    const q = possible(R.mH, R.mL);
    winPos(R.pH, R.pL, R.mH, R.mL);
    const rwH = wH, rwL = wL;
    const play0 = [], cpH = [], cpL = [], cmH = [], cmL = [], lo = new Array(W).fill(null), hi = new Array(W).fill(null);
    const hs = new Array(W).fill(null);
    for (let c = 0; c < W; c++) {
      const bH = q.H & colH[c], bL = q.L & colL[c];
      if (!(bH | bL)) continue;
      play0.push(c);
      cpH[c] = R.pH ^ R.mH; cpL[c] = R.pL ^ R.mL; cmH[c] = R.mH | bH; cmL[c] = R.mL | bL;
      if ((rwH & bH) | (rwL & bL)) lo[c] = hi[c] = (SIZE + 1 - n) >> 1;
      else { const nc = n + 1; lo[c] = -((SIZE + 1 - nc) >> 1); hi[c] = (SIZE - nc) >> 1; }
    }
    // Precomputed book entry -> exact scores, or (weak book) win/draw/loss bounds per move
    if (book && n <= book.depth) {
      const ck = canonKey(R), e = book.data[ck.key];
      if (e) {
        const sc = e.slice(); if (ck.mir) sc.reverse();
        for (const c of play0) {
          const v = sc[c]; if (v === null || v === undefined) continue;
          if (!book.weak) lo[c] = hi[c] = v;
          else if (v > 0) { if (lo[c] < 1) lo[c] = 1; } else if (v < 0) { if (hi[c] > -1) hi[c] = -1; } else lo[c] = hi[c] = 0;
        }
        if (play0.every((c) => lo[c] === hi[c])) {
          let b = play0[0]; for (const c of play0) if (lo[c] > lo[b]) b = c;
          const out = new Array(W).fill(null); for (const c of play0) out[c] = lo[c];
          post({ scores: out, exact: true, final: true, book: true, best: b, outcome: lo[b] > 0 ? "win" : lo[b] < 0 ? "loss" : "draw" });
          return null;
        }
      }
    }
    // 8-ply book at the root: the position's own win/draw/loss caps every move (draw: none wins, loss: all lose)
    if (d8 !== null && n === B8_N) {
      const w8 = d8Score(R.pH, R.pL, R.mH, R.mL);
      if (w8 <= 0) for (const c of play0) if (lo[c] < hi[c] && lo[c] <= w8 && hi[c] > w8) hi[c] = w8 < 0 ? -1 : 0;
    }
    const heurBest = () => {
      let b = -1;
      for (const c of play0) if (hs[c] !== null && (b < 0 || hs[c] > hs[b])) b = c;
      return b >= 0 ? b : play0.slice().sort((a, b2) => Math.abs(a - 3) - Math.abs(b2 - 3))[0];
    };

    const exactFrom = opts.exactFromMove === undefined ? (dbk !== null ? 4 : 7) : opts.exactFromMove;
    const hopeless = n < exactFrom;
    // With the 8-ply book, win/draw/loss of every move is cheap even where exact scores are out of reach:
    // run the classification stages only ("weak" result) instead of giving up after the heuristic phase.
    const weakOnly = hopeless && d8 !== null && !opts.helpers;
    const heurMs = opts.approxMs || (hopeless && !weakOnly ? total * (opts.helpers ? 0.55 : 1) : Math.min(900, total * 0.13));
    const heurEnd = Math.min(hardEnd, t0 + heurMs);
    const hsv = (c) => (hs[c] === null ? -1e9 : hs[c]);
    const byHeur = (a, b) => hsv(b) - hsv(a);
    const rank = (a, b) => (lo[b] - lo[a]) || (hi[b] - hi[a]) || byHeur(a, b);
    const pickBest = () => play0.slice().sort(rank)[0];
    const bestInfo = () => {
      const b = pickBest();
      let proven = true;
      for (const c of play0) if (c !== b && hi[c] > lo[b]) { proven = false; break; }
      const outcome = lo[b] > 0 ? "win" : hi[b] < 0 ? "loss" : (lo[b] === 0 && hi[b] === 0) ? "draw"
        : lo[b] >= 0 ? "win-or-draw" : hi[b] <= 0 ? "draw-or-loss" : "unknown";
      return { best: b, bestProven: proven, outcome };
    };
    const allExact = () => play0.every((c) => lo[c] === hi[c]);
    const snap = (final, extra) => Object.assign({ final, lo: lo.slice(), hi: hi.slice() }, bestInfo(), extra || {});

    let stage = -1, finished = false, stepEnd = Infinity, hdepth = 1, hdone = false;
    const lim = () => Math.min(hardEnd, stepEnd);

    // ---- Phase 1: iterative-deepening heuristic search (fallback + move ordering); one depth per call ----
    function heurStep() {
      if (now() >= heurEnd || hdepth > Math.min(SIZE - n, 60)) { hdone = true; return; }
      nodes = 0; deadline = heurEnd;
      try {
        const s = new Array(W).fill(null);
        const ord = play0.slice().sort((a, b) => hsv(b) - hsv(a));
        for (const c of ord) {
          if ((rwH & q.H & colH[c]) | (rwL & q.L & colL[c])) { s[c] = 10000 - n; continue; }
          s[c] = -hneg(hdepth - 1, cpH[c], cpL[c], cmH[c], cmL[c], n + 1, -INF, INF);
        }
        for (const c of play0) hs[c] = s[c];
        post({ scores: s, exact: false, depth: hdepth, final: false, best: heurBest() });
        let m = -INF; for (const c of play0) if (s[c] > m) m = s[c];
        hdepth++;
        if (Math.abs(m) >= 9000) hdone = true;
      } catch (e) { if (e !== TIMEOUT) throw e; hdone = true; }
    }

    // ---- Phase 2 helpers: weak classify / exact refine of root moves ----
    const classify = (c) => {
      const r = negamax(cpH[c], cpL[c], cmH[c], cmL[c], n + 1, -1, 1);
      if (r >= 1) { if (-r < hi[c]) hi[c] = -r; }
      else if (r <= -1) { if (-r > lo[c]) lo[c] = -r; }
      else lo[c] = hi[c] = 0;
      return true;
    };
    const refine = (c, skipDominated) => {
      let cmin = -hi[c], cmax = -lo[c];
      while (cmin < cmax) {
        if (skipDominated) {
          let bl = -INF; for (const d of play0) if (d !== c && lo[d] > bl) bl = lo[d];
          if (hi[c] <= bl) return true;
        }
        let med = cmin + ((cmax - cmin) >> 1);
        if (med <= 0 && Math.trunc(cmin / 2) < med) med = Math.trunc(cmin / 2);
        else if (med >= 0 && Math.trunc(cmax / 2) > med) med = Math.trunc(cmax / 2);
        const r = negamax(cpH[c], cpL[c], cmH[c], cmL[c], n + 1, med, med + 1);
        if (r <= med) cmax = r; else cmin = r;
        lo[c] = -cmax; hi[c] = -cmin;
      }
      return true;
    };
    // round-robin with growing time slices so one hard move cannot starve the others; true = list finished
    const work = (list, fn, stop) => {
      let pending = list.slice();
      while (pending.length && now() < lim() && !(stop && stop())) {
        const slice = Math.max(120, (hardEnd - now()) / pending.length), next = [];
        for (const c of pending) {
          if (now() >= lim() || (stop && stop())) { next.push(c); continue; }
          deadline = Math.min(lim(), now() + slice);
          let done = false;
          try { done = fn(c); } catch (e) { if (e !== TIMEOUT) throw e; }
          if (!done) next.push(c);
        }
        pending = next;
      }
      return pending.length === 0;
    };
    const needClassify = () => play0.filter((c) => lo[c] < hi[c] && lo[c] <= 0 && hi[c] >= 0 && !(lo[c] === 0 && hi[c] === 0)).sort(byHeur);
    const hasWin = () => play0.some((c) => lo[c] > 0);
    const finish = (extra) => {
      finished = true;
      if (allExact()) {
        const sc = new Array(W).fill(null); for (const c of play0) sc[c] = lo[c];
        post(Object.assign({ scores: sc, exact: true, final: true }, bestInfo()));
      } else post(snap(true, Object.assign({ exactTimeout: true }, extra || {})));
    };

    function step(until) {
      stepEnd = until === undefined ? Infinity : until;
      if (finished) return true;
      if (stage === -1) {
        do { heurStep(); } while (!hdone && now() < stepEnd);
        if (!hdone) return false;
        if (hopeless && !opts.helpers && !weakOnly) { post({ final: true, exactSkipped: true, best: heurBest() }); finished = true; return true; }
        stage = 0; nodes = 0;
      }
      while (!finished) {
        if (now() >= hardEnd) { finish(); break; }
        if (now() >= stepEnd) return false;
        if (stage === 0) {            // A: look for a winning move (otherwise classify everything)
          const d = work(needClassify(), classify, hasWin);
          if (d || hasWin()) { post(snap(false, { stage: "classified" })); stage = weakOnly ? 2 : 1; }
        } else if (stage === 1) {     // B: among winning moves find the quickest win
          if (work(play0.filter((c) => lo[c] > 0 && lo[c] < hi[c]).sort(rank), (c) => refine(c, true))) {
            post(snap(false, { stage: "best-move" })); stage = 2;
            if (opts.stopWhenBestProven && bestInfo().bestProven) finish({ stopped: true });
          }
        } else if (stage === 2) {     // C: classify the remaining moves
          if (work(needClassify(), classify)) { stage = 3; if (weakOnly) finish({ exactTimeout: false, exactSkipped: true, weak: true }); }
        } else if (stage === 3) {     // D: exact scores for all columns
          if (work(play0.filter((c) => lo[c] < hi[c]).sort(rank), (c) => refine(c, false))) finish();
        }
      }
      return finished;
    }
    return { step };
  }

  function analyse(cols, post, opts) {
    const S = session(cols, post, opts);
    if (S) S.step();
  }

  // ---------------- opening book ----------------
  let book = null;                       // { depth, data: { canonicalKey: [7 scores | null] } }
  function canonKey(s) {
    let k1 = 0, k2 = 0, f1 = 1, f2 = 4398046511104;
    for (let c = 0; c < 7; c++) {
      const v = cb(s.pH, s.pL, 7 * c) + cb(s.mH, s.mL, 7 * c) + 1;
      k1 += v * f1; k2 += v * f2; f1 *= 128; f2 /= 128;
    }
    return k2 < k1 ? { key: String(k2), mir: true } : { key: String(k1), mir: false };
  }
  function loadBook(b) { book = b && b.data ? b : null; return book ? Object.keys(book.data).length : 0; }

  // exact score of every legal move, no time limit (used by the book generator)
  function solveAll(cols, weak) {
    const st = setup(cols); if (!st.ok) return null;
    const R = cs, n = R.n; ctl = null; deadline = Infinity; nodes = 0;
    const q = possible(R.mH, R.mL); winPos(R.pH, R.pL, R.mH, R.mL);
    const rwH = wH, rwL = wL, out = new Array(W).fill(null);
    for (let c = 0; c < W; c++) {
      const bH = q.H & colH[c], bL = q.L & colL[c];
      if (!(bH | bL)) continue;
      if ((rwH & bH) | (rwL & bL)) { out[c] = weak ? 1 : (SIZE + 1 - n) >> 1; continue; }
      const nc = n + 1;
      if (weak) { const r = negamax(R.pH ^ R.mH, R.pL ^ R.mL, R.mH | bH, R.mL | bL, nc, -1, 1); out[c] = r >= 1 ? -1 : r <= -1 ? 1 : 0; continue; }
      out[c] = -solveRange(R.pH ^ R.mH, R.pL ^ R.mL, R.mH | bH, R.mL | bL, nc, -((SIZE - nc) >> 1), (SIZE + 1 - nc) >> 1);
    }
    return out;
  }

  // ---------------- multi-threading (shared transposition table, "lazy SMP") ----------------
  const ORD_VARIANTS = [[3, 2, 4, 1, 5, 0, 6], [3, 4, 2, 5, 1, 6, 0], [2, 3, 4, 1, 5, 0, 6], [4, 3, 2, 5, 1, 0, 6],
    [3, 2, 4, 5, 1, 6, 0], [3, 4, 2, 1, 5, 0, 6], [2, 4, 3, 1, 5, 6, 0], [4, 2, 3, 5, 1, 0, 6]];
  function setTT(sab, bits, ctlSab) {
    tt = new Int32Array(sab); ttBits = bits;
    ctl = ctlSab ? new Int32Array(ctlSab) : null;
  }
  // Runs inside a helper worker: solve root children (rotated by helper index) purely to fill the shared table.
  function helperRun(job) {
    const st = setup(job.cols); if (!st.ok) return;
    ORD = ORD_VARIANTS[job.idx % ORD_VARIANTS.length].slice();
    ctlGen = job.gen; nodes = 0;
    deadline = now() + (job.endEpoch - Date.now());
    const R = cs, n = R.n, q = possible(R.mH, R.mL);
    winPos(R.pH, R.pL, R.mH, R.mL);
    const rwH = wH, rwL = wL, kids = [];
    for (let c = 0; c < W; c++) {
      const bH = q.H & colH[c], bL = q.L & colL[c];
      if (!(bH | bL) || ((rwH & bH) | (rwL & bL))) continue;
      kids.push({ pH: R.pH ^ R.mH, pL: R.pL ^ R.mL, mH: R.mH | bH, mL: R.mL | bL });
    }
    if (!kids.length) return;
    const rot = kids.map((_, i) => kids[(i + job.idx) % kids.length]);
    const nc = n + 1;
    try {
      for (const k of rot) negamax(k.pH, k.pL, k.mH, k.mL, nc, -1, 1);                         // win/draw/loss
      for (const k of rot) solveRange(k.pH, k.pL, k.mH, k.mL, nc, -((SIZE - nc) >> 1), (SIZE + 1 - nc) >> 1); // exact
    } catch (e) { if (e !== TIMEOUT) throw e; }
  }
  // Main side: create once, then call pool.analyse(...) instead of analyse(...).
  // Needs a worker script that loads this file (see solver-worker.js) and SharedArrayBuffer
  // (cross-origin isolation). Returns null when unavailable -> use plain analyse().
  function createPool(o) {
    o = o || {};
    if (typeof SharedArrayBuffer === "undefined") return null;
    if (typeof crossOriginIsolated !== "undefined" && !crossOriginIsolated) return null;
    const WC = o.WorkerCtor || (typeof Worker !== "undefined" ? Worker : null);
    if (!WC || !o.workerUrl) return null;
    const hc = (typeof navigator !== "undefined" && navigator.hardwareConcurrency) || 4;
    const threads = o.threads || Math.max(1, hc - 2), bits = o.ttBits || 24;
    const sab = new SharedArrayBuffer(8 << bits), csab = new SharedArrayBuffer(16), c = new Int32Array(csab);
    setTT(sab, bits);
    const ws = [];
    for (let i = 0; i < threads; i++) { const w = new WC(o.workerUrl); w.postMessage({ type: "init", tt: sab, bits, ctl: csab }); ws.push(w); }
    return {
      threads,
      analyse(cols, post, opts) {
        opts = Object.assign({}, opts || {}); opts.helpers = threads;
        const total = opts.totalMs || 7000, endEpoch = Date.now() + total, gen = Atomics.add(c, 0, 1) + 1;
        ws.forEach((w, i) => w.postMessage({ type: "job", cols, endEpoch, idx: i, gen }));
        try { return analyse(cols, post, opts); } finally { Atomics.add(c, 0, 1); }   // stop helpers
      },
      terminate() { ws.forEach((w) => w.terminate && w.terminate()); },
    };
  }

  function fromBoard(b) {            // b[col][row] in {0,1,2}; 1 = player 1 (moved first)
    let pH = 0, pL = 0, mH = 0, mL = 0, n = 0;
    for (let c = 0; c < W; c++) for (let r = 0; r < H; r++) if (b[c][r]) n++;
    const mover = n & 1 ? 2 : 1;
    for (let c = 0; c < W; c++) for (let r = 0; r < H; r++) {
      const v = b[c][r]; if (!v) continue;
      const bit = c * 7 + r, hi = bit >= 32, m = 1 << (hi ? bit - 32 : bit);
      if (hi) { mH |= m; if (v === mover) pH |= m; } else { mL |= m; if (v === mover) pL |= m; }
    }
    cs = { pH, pL, mH, mL, n }; stack = [];
  }

  const api = {
    analyse, session, createPool, setTT, helperRun, loadBook, solveAll, canonKey: (cols) => (setup(cols).ok ? canonKey(cs) : null),
    loadDatBook: dbLoad,
    datBookSize: () => (dbk ? dbk.n : 0),
    shareDatBook: () => (dbk ? { keys: dbk.keys.buffer, vals: dbk.vals.buffer } : null),   // pass to helper workers
    setDatBook: (k, v) => { const keys = new Int32Array(k), vals = new Int8Array(v); dbk = { keys, vals, n: keys.length }; },
    datScore: (cols) => { if (!dbk || !setup(cols).ok || cs.n !== BK_N) return null; const r = dbScore(cs.pH, cs.pL, cs.mH, cs.mL); return r === BK_NONE ? null : r; },
    loadDat8Book: d8Load,
    dat8BookSize: () => (d8 ? d8.n : 0),
    shareDat8Book: () => (d8 ? { keys: d8.keys.buffer, vals: d8.vals.buffer } : null),   // pass to helper workers
    setDat8Book: (k, v) => { const keys = new Int32Array(k), vals = new Uint8Array(v); d8 = { keys, vals, n: keys.length }; },
    dat8Score: (cols) => { if (!d8 || !setup(cols).ok || cs.n !== B8_N) return null; return d8Score(cs.pH, cs.pL, cs.mH, cs.mL); },
    _db8Off: () => { d8 = null; },
    _db8ScoreOfBoard: (b) => { fromBoard(b); return d8 ? d8Score(cs.pH, cs.pL, cs.mH, cs.mL) : null; },
    _weak: () => { nodes = 0; deadline = Infinity; const s = cs; return negamax(s.pH, s.pL, s.mH, s.mL, s.n, -1, 1); },
    _dbOff: () => { dbk = null; },
    _dbCodeOfBoard: (b, rev) => { fromBoard(b); return dbEnc(cs.pH, cs.pL, cs.mH, cs.mL, !!rev); },
    _dbScoreOfBoard: (b) => { fromBoard(b); return dbk ? dbScore(cs.pH, cs.pL, cs.mH, cs.mL) : BK_NONE; },
    _fromBoard: fromBoard,
    _setup: setup,
    _solve: () => { nodes = 0; deadline = Infinity; const s = cs; return solveRange(s.pH, s.pL, s.mH, s.mL, s.n, -22, 22); },
    _ab: (d) => { nodes = 0; deadline = Infinity; const s = cs; return hneg(d, s.pH, s.pL, s.mH, s.mL, s.n, -INF, INF); },
    _play: (c) => { stack.push(Object.assign({}, cs)); return step(cs, c); },
    _undo: () => { cs = stack.pop(); },
    _state: () => ({ n: cs.n, cur: (cs.n & 1) + 1 }),
    _cfg: (o) => { if (o.symn !== undefined) SYMN = o.symn; if (o.ttBits) { ttBits = o.ttBits; tt = new Int32Array(2 << ttBits); } },
    _nodes: () => nodes,
    _reset: () => { tt.fill(0); deadline = Infinity; },
  };
  if (typeof module !== "undefined" && module.exports) module.exports = api; else root.C4 = api;
})(typeof self !== "undefined" ? self : this);