@AGENTS.md

## Versioning
- Change the version number in this project's (leanscope-agent-kit-angular-quickstart) package.json after each session (only in case there was a commit in between!). For minor things (fixes and mini enhancements), increase the third number, for major update that lead to a committed plan change the second number. The first number I will change manually, as it stands for major upGrades, not majore upDates.
- Track all major updates and upgrades in a RELEASES.md release notes file.

## Spelling

Always write **LeanScope** with a capital L and a capital S: in prose, comments, UI text and in
PascalCase or camelCase identifiers (`LeanScopeConfig`, `LeanScopeSessionService`,
`provideLeanScope`). Never write `Leanscope` or `Lean Scope`.

Exceptions, where the casing is fixed by the convention of the context:
- all lowercase: file and folder names, URLs, package and repository names, CSS classes
  (`leanscope-session.service.ts`, `leanscope-agent-kit`, `app.leanscope.ai`)
- all caps: constants and environment variables (`LEANSCOPE_CONFIG`, `LEANSCOPE_OPENAI_API_KEY`)
