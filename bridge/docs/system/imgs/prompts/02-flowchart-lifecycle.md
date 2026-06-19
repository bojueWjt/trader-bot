---
type: flowchart
style: sketch-notes
palette: warm
filename: 02-lifecycle.svg
language: zh
---

# Prompt

Create a hand-drawn flowchart showing the signal lifecycle for Hermes Trader. Use cream paper, strong black outlines, pastel status blocks, and concise Chinese labels.

Flow:
- raw Telegram message
- parsed signal
- needs_review or approved
- reserved entry
- sent_to_freqtrade
- entered
- partial exit
- exited

Side branches:
- rejected
- expired
- duplicate_operation
- manual_review_required

Aspect: wide 16:9 with a clear left-to-right path.
