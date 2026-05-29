# ORB Strategy V2 — Futures + Options

**Balfund Trading Private Limited**

Two independent trading systems using Opening Range Breakout with RSI filter.

## Systems

| System | Chart | Entry | Target/lot | SL/lot | TSL Step |
|--------|-------|-------|-----------|--------|----------|
| Futures | Index Futures | Breakout + RSI>50 (Buy) / RSI<50 (Sell) | ₹3,000 | ₹1,500 | ₹500 |
| Options | CE & PE Premium | Breakdown + RSI<50 → Sell | ₹2,000 | ₹1,000 | ₹400 |

## Quick Start

```bash
pip install -r requirements.txt
python gui.py
```

## EXE Build

Push to GitHub → Actions builds automatically → Download EXE from Artifacts.
