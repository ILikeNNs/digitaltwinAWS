### Summary

This subdirectory contains code related to my digital twin, an LLM capable of answering questions about my resume. It has the full pipeline - including parsing the PDF and chunking it to load it into a Chroma database.

### Agents used

There are two tools used in the project:
- a tool that does retrieval-augmented generation from the PDF file containing my resume
- a tool that allows people to leave their contact information if they want to chat with me

### Modules written

The project is modularized (as it should be):
- **app.py** contains code for the Gradio-based web UI
- **database.py** contains code for setting up an sqlite database, as well as code for logging the conversations and contact details of interlocutors (in a separate table)
- **helpers.py** contains tool definitions
- **rag_engine.py** contains a RAGEngine class which contains methods for chunking the resume text, as well as retrieving the relevant context/chunks for a given question

### Deploying to AWS

Two CloudFormation deployments are provided. Both default to **eu-north-1 (Stockholm)**; pass `-Region` to change it. App data never leaves the chosen EU region. The only thing created in us-east-1 is the optional WAF web ACL (`infra/waf.yaml`), because AWS requires CloudFront web ACLs to live there. It holds rules only, no data.

OpenAI usage is billed separately and isn't included in the AWS costs below. Before deploying either version, put a **budget/usage limit on the OpenAI project** whose key you use (OpenAI dashboard). It's the only control that caps OpenAI spend itself; everything below limits request rates.

The `deploy*.ps1` scripts are thin wrappers around `infra/deploy.py`, which holds the build, upload and CloudFormation logic so it runs identically on your machine and on CodeBuild. It needs [uv](https://docs.astral.sh/uv/); on macOS/Linux call it directly, e.g. `uv run --group deploy python infra/deploy.py deploy serverless` (`--help` lists the steps the pipeline uses individually).

#### Option 1: Gradio on EC2 (`infra/digitaltwin.yaml`)

```
viewer ─HTTPS─▶ CloudFront (+WAF) ─VPC origin─▶ EC2 in a private subnet (Amazon Linux 2023, t4g.small)
                                                  ├─ egress via NAT Gateway (OpenAI, PyPI, AWS APIs)
                                                  ├─ app zip from S3, pinned by object version + SHA-256, installed by cfn-init (uv)
                                                  ├─ OPENAI_API_KEY: Secrets Manager, read at service start
                                                  ├─ conversation_logs.db + chroma_db: separate encrypted EBS volume (retained)
                                                  └─ dnf-automatic security updates + weekly reboot-if-needed window
```

- A single instance, because SQLite and the local Chroma index don't support multiple writers.
- The instance has no public IP. CloudFront reaches it through a VPC origin, so app traffic never crosses the internet unencrypted. There's no SSH; use SSM Session Manager.
- Replies stream into the chat as the model writes them (Gradio generator handler; CloudFront passes the stream through).
- Updates: re-run the deploy script. `cfn-hup` notices the new package within about a minute, verifies its hash and restarts the app without replacing the instance.

```powershell
.\infra\deploy.ps1                 # eu-north-1, with WAF
.\infra\deploy.ps1 -NoWaf          # skip WAF (saves ~$9/month, loses per-IP rate limiting)
```

Cost (eu-north-1 list prices, Oct 2026): t4g.small $12.56 + NAT Gateway $33.58 + NAT IPv4 address $3.65 + EBS $1.50 + WAF $9.00 + secret $0.40, about **$61/month** ($52 with `-NoWaf`). It's almost all fixed cost, so it barely changes with traffic.

#### Option 2: Serverless (`infra/serverless.yaml`)

Gradio's queue needs long-lived connections that stay on one process, which Lambda can't provide. This version therefore swaps the Gradio UI for a small static chat page (`serverless/web/`) and shares the persona, model settings, tools, limits and the streaming tool-call loop with the Gradio app through `helpers.py`.

Replies stream token by token. Python has no native Lambda response streaming, so `serverless/server.py` is a small Starlette app run by the **Lambda Web Adapter** layer (`serverless/run.sh`), exposed through a **Function URL in `RESPONSE_STREAM` mode**. API Gateway HTTP APIs can't stream Lambda responses, so there's no API Gateway.

```
browser ─▶ CloudFront (+WAF, strict CSP) ─┬─ /        ─▶ S3 (private, Origin Access Control)
                                          └─ /api/*   ─▶ Lambda Function URL (AWS_IAM, Origin Access Control, RESPONSE_STREAM)
                                                           └─ Lambda Web Adapter ─▶ Starlette (Python 3.13, arm64, reserved concurrency)
                                                                ├─ OpenAI, streamed (key in Secrets Manager)
                                                                ├─ embeddings.json (RAG, in-memory)
                                                                └─ DynamoDB ─ stream ─▶ EventBridge Pipe ─▶ SNS (KMS) ─▶ email
```

| Concern | Choice |
|---|---|
| RAG | The resume is embedded once at build time (`serverless/package.py`) and searched in memory. A few dozen chunks don't justify a vector database, and dropping `chromadb` keeps the Lambda a ~6 MB zip built without Docker. Dependencies are pinned to `uv.lock`. |
| Storage | A single DynamoDB table: `CONVERSATION` and `CONTACT` records keyed by timestamp, plus `SESSION#<id>` items holding the server-side chat history (24h TTL). On-demand billing, point-in-time recovery, deletion protection. |
| Notifications | A DynamoDB stream feeds an EventBridge Pipe that filters for new contact requests and emails them through a KMS-encrypted SNS topic, with a warning that the content is unverified visitor input. |
| Streaming | The browser posts only its newest message and reads an NDJSON stream (`{"sessionId"}`, `{"delta"}`…, `{"done"}`). It also sends the body's SHA-256 in `x-amz-content-sha256`, which Lambda requires for POSTs signed by CloudFront's Origin Access Control. |
| Security | The Function URL uses `AWS_IAM` auth and only CloudFront (Origin Access Control) may invoke it, so nothing can reach the Lambda without passing WAF. The Lambda can write records but may only read `SESSION#` items and only **count** contacts (IAM `dynamodb:LeadingKeys` + `dynamodb:Select` conditions), so it can't read stored contacts back. Strict CSP, private S3 origin, TLS everywhere. |
| Observability | Structured JSON logs, X-Ray tracing, and alarms for Lambda errors, Function URL 5xx and sustained throttling. |

```powershell
.\infra\deploy-serverless.ps1 -NotificationEmail you@example.com
```

Requires `OPENAI_API_KEY` in `.env`, which is used to embed the resume at build time. The deploy script reserves 5 concurrent executions as a global cap. If the account's Lambda concurrency quota is too low to reserve any (new accounts can start at 10), it deploys without the cap and warns.

Cost (eu-north-1 list prices, Oct 2026): about **$10.40/month** ($1.40 with `-NoWaf`), almost all of it the WAF ($9), the KMS key ($1, only with `-NotificationEmail`) and the OpenAI secret ($0.40). Usage-based charges stay under $1/month even at 50,000 messages, thanks to Lambda's always-free tier.

#### Tearing down

```powershell
.\infra\destroy-serverless.ps1                          # serverless stack, its site bucket and WAF stack
.\infra\destroy.ps1                                     # EC2 stack and its WAF stack
.\infra\destroy.ps1 -DeleteData -DeleteShared           # ...plus the data volume, OpenAI secret and artifact bucket
```

Both scripts list what they will delete and ask you to type the stack name first (`-Force` skips the prompt for automation). They delete the app stack before its WAF stack (CloudFront must release the web ACL first) and are safe to re-run after a partial failure.

Kept unless you ask: the **data** (DynamoDB table or EBS volume holding contact requests) needs `-DeleteData`, and the **shared** OpenAI secret and artifact bucket need `-DeleteShared`. Even then, shared resources are only removed once neither app stack exists. The secret gets a 7-day recovery window (`aws secretsmanager restore-secret`).

#### Abuse and cost guards (both versions)

| Guard | Where |
|---|---|
| Per-IP rate limit on POSTs (60 per 5 min for Gradio, 30 for serverless), IP reputation and common-exploit rules | WAF (`infra/waf.yaml`) |
| Global cap on simultaneous replies | Lambda reserved concurrency (serverless) |
| Conversation history kept server-side; the browser can't inject `system`/`assistant` turns | `gr.State` / DynamoDB sessions |
| Max 2,000 characters per message, 20 turns sent to the model, 30 user turns per session, 1,000 completion tokens per call | `helpers.py` |
| Contact tool: email format check and at most 10 requests per hour globally | `database.py` / `serverless/server.py` |
| Generic error replies (details only in server logs); logging failures never break the chat | `app.py` / `serverless/server.py` |
| Gradio endpoints private (not part of the public Gradio API) | `app.py` |

Both stacks share the versioned artifact bucket and the OpenAI secret, so they can run side by side. Deleting a stack keeps its data (the EBS volume or the DynamoDB table); delete those manually if you no longer need the logs.
