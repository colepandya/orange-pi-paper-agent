#!/bin/bash
echo "== timers =="; systemctl list-timers 'paper-agent*' --no-pager 2>/dev/null | head -12
echo "== agent =="; sudo -u paperagent env PYTHONPATH=/opt/paper-agent python3 -m paper_agent status
echo "== dashboard health =="; curl -s localhost:8088/health; echo
echo "== last cycle log =="; journalctl -u paper-agent-cycle -n 15 --no-pager 2>/dev/null
