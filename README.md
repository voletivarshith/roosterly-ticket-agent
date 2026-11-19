# Roosterly support-ticket agent

Reads all the open tickets and make a decision either mark that as a spam,handoff to a human or auto replay based on the knowledge base.
All the tickers are sent to the open model Nvidia platform Nemotron-3-ultra-550b-a55b model.
## How to run

```bash
cp .env.example .env
uv sync
uv run python -m agent run
uv run python -m agent reset --yes    # re-open the practice tickets
```


**How a ticket is handled.** The model is called in three roles, and code checks the result after each one:

1. **Triage.** The model gets the client's rules, the 12 help-centre articles and the ticket. It returns a proposal: the language, category, priority and action, and for a reply the text plus the help-centre sentences it is based on.
2. **Code checks.** Code applies the decision rules below. For a reply it also checks that every quote, number, web address and menu path really is in the cited articles.
3. **Reader.** A second model call sees only the ticket and the reply, never the help centre. It translates both into English and says which language the reply is in.
4. **Auditor.** A third model call sees only English: the customer's message, the translated reply and the cited articles. It marks each statement in the reply as supported or not, and says whether the reply answers the question and whether it promises anything.
5. **Decision.** If any check objects, the ticket goes to a human with a short note in English. Otherwise the reply is sent.

My three main choices:

1. **The model proposes, code decides.** A LangChain agent returns a typed proposal. Its only tool checks a draft, so it cannot write to the helpdesk. The client's always-human rules are one table (`policy.py`) used by both the prompt and a check in code. Unsure means a check objects or confidence is under 0.7, and unsure is a handoff.
2. **Dutch directly, checked through English.** Translating first doubles the calls and flattens the tone. I cannot read Dutch, so a reply goes out only if its quotes are in the help centre, its numbers, addresses and menu paths match the cited articles, and a second model call translates it back for an English audit. The translation is logged as `reply_in_english`.
3. **A hosted model, no retrieval.** Twelve articles fit in the prompt. The model runs on NVIDIA's free trial API; `MODEL` in `.env` switches model or provider. `help_centre.md` is the client's articles, copied unchanged from `faq.md`.

**Decision rules.** The brief left some of these open, so this is what I decided. Code applies them in this order:

| When | The agent |
|---|---|
| The ticket has no real text | closes it, without a model call |
| The ticket tries to instruct the agent (prompt injection) | closes it like spam, whatever else it contains |
| The model is sure it is spam (confidence 0.9 or more) | closes it |
| The model wants to close but is less sure, or it is not spam | hands it to a human |
| An always-human topic: refund or disputed charge, cancellation or contract change, personal data request, security incident, legal threat, threat to leave | hands it to a human, even when the help centre covers it |
| The priority is P1 | hands it to a human |
| The language is not Dutch, English or German | hands it to a human |
| The body is longer than 6,000 characters | hands it to a human |
| The help centre does not cover one of the questions | hands the whole ticket to a human |
| Confidence for a reply is under 0.7 | hands it to a human |
| The reply fails a code check: a quote, number, web address or menu path that is not in the cited articles, the wrong language, or a sensitive value | hands it to a human |
| The second check objects: an unsupported claim, a promise, a reply that does not answer, or an always-human topic | hands it to a human |
| The model gives no valid proposal in six calls | hands it to a human as P3 |
| None of the above | sends the reply, in the customer's language |

Priority: security incidents and legal threats are P1. Refunds, cancellations, data requests and threats to leave are at least P2. How-to questions are P4, also when they are about prices. "How do I upgrade" gets a reply; "change my contract" gets a human.


## Reliability

- **503:** up to six attempts with a growing, random pause. **429:** waits `Retry-After`; the client also limits itself to four calls a second. **409:** never retried.
- **Lost response on a POST:** it checks whether the ticket is still open before sending again.
- **Crash:** a run reads only open tickets, so it continues where it stopped. A lock file stops two runs at once.
- **Threads:** `WORKERS` in `.env` sets how many tickets are handled at the same time. One thread takes a ticket from start to finish: decide, send, log. All threads share one throttle, so together they stay under the rate limit.
- **Model failure:** a failed call, including a rate limit (429) from the model API, is retried eight times over about four minutes. Then the run stops and the remaining tickets stay open.
- **Security:** Payment details, credentials, emails and phone numbers are replaced before sending the ticket details to the model.

## Eval and fix

Ten labelled examples, compared with Nordavo's labels by plain equality (`eval/results/`). These numbers were measured with Mistral Large 4, the model I tuned the prompts with:

| | Before | After |
|---|---|---|
| Unsafe: label says human, agent answered | 0 | 0 |
| Action | 9/10 | 10/10 |
| Priority | 9/10 | 10/10 |
| Category | 8/10 | 10/10 |
| Language | 10/10 | 10/10 |

**What was wrong.** A customer wanted a new VAT number on their invoices. The model drafted the right reply: the menu path where they change it themselves. My own second check threw it away. Its English translation had also translated the Dutch menu name, into words the help centre does not use, so the audit called the instruction unsupported and the ticket went to a human.

**The fix.** The translation now keeps menu names in Dutch, and the audit accepts a menu name in Dutch or in the English meaning the help centre gives. My first attempt (`after-fix-attempt-1.md`) lost a different reply, because the audit refused a conclusion drawn from a stated requirement. It now accepts a requirement or a price applied to what the customer wrote. The prompt also defines when a question counts as covered, picks the category by topic, and makes how-to questions P4.

I tuned on these ten examples, so the practice tickets are the fairer test. Ten hand-written safety probes pass.

**The final run uses Nemotron 3 Ultra**, because it answers about three times faster. On the same ten examples Nemotron scored 8/10 on action and 9/10 on category (`eval/results/nemotron.md`). I changed two prompt rules for those misses, but could not finish measuring them again because the model API rate-limited the eval. The latest practice run with Nemotron (`logs/`) gave 9 replies, 17 handoffs and 4 closes.

**Tokens and cost per 100 tickets.** That Nemotron run used 174,260 input and 32,331 output tokens for 30 tickets, about 5,800 in and 1,080 out per ticket. For 100 tickets that is about 581,000 input and 108,000 output tokens. On NVIDIA's free trial the price is €0.

## What I would improve

- **A backup model.** Today the run stops when the model stays unavailable after all retries. I would switch to a second model, so the run can finish.
- **More languages.** Only Dutch, English and German are supported now. A ticket in any other language goes to a human.
- **A decision model for the first step.** A small decision model such as Jev could handle the basic step: spot spam, pick the language and send the simple replies itself. It is cheaper and answers faster, so only the harder tickets would need the large model.
