# Autonomous Coding Agent (Name not finalized)
## Architecture
1. Invocation Model: On-demand -> Long-running daemon
2. Action Model: Trust tiers per action class (Read-only actions auto-allowed; reversible writes (file edits in a git repo) auto-allowed with logs; irreversible actions (push, send email, delete) require confirmation.)
3. Long lasting context: 

## How to use
### Current
- CLI Endpoint (Not yet decided)

### Future Plans
- VSCode extension

## Focuses
- Context Management (To explore)
    - Ochestrator / Subagent workflow (Delegate tasks to subagents; Keep context light in the main agent; Return outcomes to the main agent) 
- Memory Management
    - H