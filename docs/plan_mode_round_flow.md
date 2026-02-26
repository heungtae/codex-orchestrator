# Plan Mode - Round별 흐름

### max_review_rounds = 1

| Round | Decision | 흐름 |
|-------|----------|------|
| 1 | | planner → dev → completed |

### max_review_rounds = 2

| Round | Decision | 흐름 |
|-------|----------|------|
| 1 | approved | planner → dev → rev -> completed |
| 2 | needs_changes | planner → dev → rev → dev → completed |

### max_review_rounds = 3

| Round | Decision | 흐름 |
|-------|----------|------|
| 1 | approved | planner → dev → rev -> completed |
| 2 | needs_changes | planner → dev → rev → dev → round3 |
| 3 | approved | rev → completed |
| 3 | needs_changes | rev → dev → completed |
