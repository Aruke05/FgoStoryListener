# FGO terminology knowledge base

`fgo_knowledge_base.json` is the bundled, offline fallback. Runtime updates are
written under `%LOCALAPPDATA%\FgoStoryListener\knowledge` and never modify the
installed copy.

## Lifecycle

1. **Acquire** source snapshots from Atlas Academy and the Mooncell MediaWiki API.
2. **Stage** parsed pairs, unresolved conflicts, and JP-only discoveries.
3. **Validate** schema, minimum page/term counts, duplicates, and source priority.
4. **Promote** with an atomic file replacement; the old active file becomes
   `previous.json`.
5. **Retrieve** only terms occurring in the current dialogue/context for each AI
   turn. Quest-specific model memory remains in `history.db` and cannot promote
   itself into this global dictionary.

## Source priority

| Priority | Data |
|---:|---|
| 100 | Manually reviewed core terminology bundled with the app |
| 92 | Mooncell structured servant/card/battle-name fields |
| 88 | Mooncell structured official-CN event names |
| 85 | Atlas Academy JP/CN game-data pairs and costumes |
| 76 | Mooncell structured Noble Phantasm fields |
| 74 | Mooncell structured skill fields |
| 65 | Mooncell community event names where the official CN field is absent |

Free-form page prose and model guesses are not auto-promoted. Equal-priority
disagreements are quarantined in `staging`, while lower-priority alternatives are
kept there as provenance rather than silently discarded.

