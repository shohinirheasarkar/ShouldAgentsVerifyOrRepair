# Can Agents Learn When to Verify or Repair Memory Under Budget Constraints and Access Uncertainty?

CMSC 848N · Fall 2026 · [GitHub repository](https://github.com/shohinirheasarkar/ShouldAgentsVerifyOrRepair)

## Project intro
Long-running agents may retain summaries built from facts that later change. When a query touches a summary flagged as stale, the agent can **verify** the current source for this answer or **repair** the stored summary for later use. We aim to compare decision policies under limited cost and uncertain future reuse. The proposal suggests a controlled event stream, simple fixed memory components, and comparisons to verify-only, eager repair, first-access repair, a threshold policy, and a hindsight oracle. Access uncertainty and a shared budget still need precise definitions. This is a research plan, not a reported finding.

## How to navigate this repo

| Location | Purpose | Status |
| --- | --- | --- |
| [README.md](README.md) | Project overview and contribution guide | Current |
| [docs/proposal.md](docs/proposal.md) | Proposal requirements and artifact links | Draft checklist |
| [docs/midterm.md](docs/midterm.md) | Midterm requirements and results placeholders | Pending |
| [docs/artifact-audit.md](docs/artifact-audit.md) | Audit rubric and reproducibility evidence | Pending |
| [docs/final-presentation.md](docs/final-presentation.md) | Presentation rubric and slides | Pending |
| [docs/final-report.md](docs/final-report.md) | Final paper checklist | Pending |
| [docs/related-works.md](docs/related-works.md) | Papers to verify and annotated notes | In progress |

Add each new file to this table when it is committed.

## How to obtain, set up, and use the code
The repository currently has **no runnable code, requirements file, installation procedure, or measured results**. To obtain its documents, use GitHub's **Code → Download ZIP**, or clone with `git clone https://github.com/shohinirheasarkar/ShouldAgentsVerifyOrRepair.git`. When code is added, record the tested Python version, exact dependencies, setup commands, a small example command, expected output, and the commands to reproduce each result here. Do not infer install instructions from the proposal's suggested libraries.

## Tutorial notebooks
None yet. If a notebook is useful later, link it here, state its inputs and expected output, and keep the reproducible experiment in a documented script where practical.

## Standards for each code file
When adding a code file, put a short header or docstring describing its purpose, inputs, outputs, and how to run it. Explain non-obvious variables and policy choices; use clear names, fixed seeds for randomized experiments, and explicit units for costs and latency. Put the exact run command and dependencies in this README. Describe which data were used for development and which were held out. Ask another teammate to review changes in a pull request.

## Progress trackers
A dependency-aware [weekly tracker](docs/weekly-tracker.md) is being drafted and will be added **after the team agrees on the schedule**. Use one GitHub Project board for Issues (assignable work) and pull requests (proposed changes to review). No board or Issues have been created or verified yet. Monday is a progress discussion; proposed Tuesday and Thursday assignments will appear in the tracker.

## Links to major documents and presentations
| Deliverable | Planning page | Finished artifact |
| --- | --- | --- |
| Proposal | [Checklist](docs/proposal.md) | Link pending |
| Midterm report | [Checklist](docs/midterm.md) | Link pending |
| Artifact audit | [Checklist](docs/artifact-audit.md) | Link pending |
| Final presentation | [Checklist](docs/final-presentation.md) | Link pending |
| Final paper-style report | [Checklist](docs/final-report.md) | Link pending |

Deadlines other than the supplied December 1 final deadline need confirmation from the course schedule.

## Related works
See [the verification and notes table](docs/related-works.md). The proposal names online ski rental, memory maintenance, model editing, and long-memory benchmarks. Treat its summaries and citation details as draft material until checked against the original papers and benchmark documentation.

## Team
- Shohini Rhea Sarkar — [@shohinirheasarkar](https://github.com/shohinirheasarkar)
- Amelia Harn — [@aharn3](https://github.com/aharn3)
- Arik Gershman — [@arikgershman](https://github.com/arikgershman)

Planned effort: about eight hours per person each week through December 1, 2026. Contributions and assignments will be recorded in the approved weekly tracker.
