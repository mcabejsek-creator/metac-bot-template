# Preserve question context across forecast stages

Numeric/MC used a different close time than research; nominal bounds could be
presented as hard limits; fact-check lost options/fine print and conditional
branches lost parent reasoning. Carry complete question contracts and source
text through research, summarization, forecasting, parsing and explanation,
without changing model settings or probability calculations.

Also retain original group/member prose, full intraday date bounds and affirmed
conditional values. Reject oversized/truncated explanations before publication;
bind pending-plan recovery to timing and other relevant source metadata.
Publication CAS, deadlines and workflows remain unchanged.

Validation: 242 offline tests pass, including strict mode and simulated Actions;
no remaining known failures or unexpected live calls. Five IDs represented six
tests because TIMING-1 covers both numeric and multiple choice.

Every necessary prompt/data change and behavior tradeoff is listed separately
in [question-context-integrity.md](question-context-integrity.md). Larger contexts
can increase token use or cause refusal; passing tests cannot prove semantic
accuracy of live model output or exactly-once delivery by Metaculus.

Local draft only: no remote PR, push, merge or deployment.
