#!/bin/bash
exec sudo -u paperagent env PYTHONPATH=/opt/paper-agent python3 -m paper_agent doctor "$@"
