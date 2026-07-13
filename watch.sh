#!/bin/bash
RUN=$(ls -1dt /home/claudeuser/loop-orchestrator/runs/*/ | head -1)
while true; do
  clear
  echo "===== LOOP ($RUN) ====="
  tail -n 4 /home/claudeuser/loop-orchestrator/orchestrator.console.log
  ITER=$(ls -1t "$RUN"/iter*_worker.stream.log 2>/dev/null | head -1)
  echo; echo "===== WORKER ACTIVITY ($(basename "$ITER")) ====="
  tail -n 8 "$ITER" 2>/dev/null | cut -c1-160
  echo; echo "===== BENCHMARK RUNNING? ====="
  ps -eo etime,cmd | grep "[d]emo.py --model-path" | head -1 || echo "(none active)"
  echo "GPU2 busy%: $(rocm-smi -d 2 --showuse 2>/dev/null | grep -oE 'use \(%\): [0-9]+' | grep -oE '[0-9]+')"
  echo; echo "===== NEWEST OUTPUT FILES ====="
  ls -lt /home/claudeuser/mirage/pp_verify_out/ 2>/dev/null | head -5
  sleep 5
done
