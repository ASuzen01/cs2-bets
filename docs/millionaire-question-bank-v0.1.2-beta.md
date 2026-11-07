# Millionaire question bank v0.1.2-beta

This release adds 18,188 retained questions to the runtime question pool. It is an intentionally broad, user-directed beta release. All records retain `qa_status=pending`; none have been represented as editorially approved.

## Runtime behavior

- New games choose one random question from each numbered stage, in order from stage 1 through 15.
- The existing 15-question round, payout schedule, checkpoints at 5 and 10, balance handling, and answer secrecy remain unchanged.
- Existing in-progress games using the original question IDs can still resolve those IDs; new games draw from the released bank.
- The runtime bank is `data/millionaire/releases/millionaire-questions-v0.1.2-beta.json.gz`.

## Contents

- OpenTDB Russian translations: 3,984
- RuBQ Russian source questions: 1,545
- Original Russian wording based on Wikidata facts: 2,547
- SberQuAD Russian source questions: 10,112
- Total: 18,188

The 43 automatically quarantined duplicate/error records are excluded.

## QA status

All 18,188 records remain pending content QA. Prior checks removed normalized prompt conflicts, structural option collisions, several known untranslated answers, and obvious English prose. They did not establish full translation quality, fact freshness, question uniqueness in meaning, or distractor validity. This is not a claim that all content has been manually reviewed.

## Tests

At release preparation, the full repository test suite passed (674 tests). Ruff passed for the modified game service and tests.
