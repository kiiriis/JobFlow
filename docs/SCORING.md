# Scoring Engine

The scoring engine (`jobflow/filter.py`) evaluates each job posting against Milan's ASIC/SoC/FPGA/GPU hardware profile and produces a normalized 0-100% match score.

## Score Calculation

```
raw = keyword_score + synergy_bonus + level_points + experience_score
      + recency_score + location_score + h1b_bonus + senior_penalty

score_pct = min(100, max(0, round(raw / 130 * 100)))
```

`SCORE_MAX_RAW = 130` is the practical ceiling.

## Scoring Signals

### 1. Keyword Matching (`keyword_score`)

Binary presence match. Each keyword scores once regardless of frequency.

| Category | Examples |
|----------|----------|
| **Design** | ASIC, SoC, RTL, VLSI, Verilog, SystemVerilog, digital design, synthesis |
| **Verification** | design verification, UVM, testbench, coverage, assertions, constrained random, simulation |
| **Physical** | physical design, STA, static timing analysis, timing closure, P&R, floorplanning, DRC/LVS |
| **FPGA/GPU** | FPGA, GPU ASIC, Xilinx, Vivado, Quartus |
| **EDA Tools** | Cadence, Synopsys, PrimeTime, Innovus, VCS, Questa, ModelSim |
| **Scripting** | C, C++, Python, Perl, Tcl, Linux, shell scripting |

### 2. Synergy Combos (`synergy_bonus`)

Extra points when a full hardware combo appears together:

| Combo | Bonus |
|-------|-------|
| SystemVerilog + UVM + coverage | +10 |
| RTL + Verilog + synthesis | +10 |
| physical design + STA + timing closure | +10 |
| FPGA + Verilog + Vivado/Quartus | +8 |
| SoC + RTL + verification | +10 |
| GPU + ASIC + RTL | +10 |
| DFT + CDC + RTL | +8 |

### 3. Level, Experience, Location

New grad and entry-level signals receive the strongest level bonus. Jobs requiring 4+ years are rejected, 0-2 years is the best fit, and US/remote-US locations are required. Explicit H1B/OPT/visa sponsorship language adds a bonus; explicit no-sponsorship, citizenship-only, green-card-only, or clearance-required language rejects the job.

## Target Role Guard

Jobs are rejected unless the title clearly matches Milan's target hardware scope: ASIC, SoC, FPGA, RTL, VLSI, GPU ASIC, design verification, physical design, STA, synthesis, DFT, CDC, silicon, semiconductor, digital design, logic design, or chip design.

Generic SWE, backend, frontend, embedded-only, firmware-only, data, ML/AI, DevOps/SRE, IT/support, product, sales, management, senior/staff/principal/lead, and software QA/testing titles are rejected.

## Variant Selection

Milan-specific hardware resume variants are not in this repo yet, so all passing roles default to the existing `se` variant for compatibility.
