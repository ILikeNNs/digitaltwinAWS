### Summary

This subdirectory contains code related to my digital twin, an LLM capable of answering questions about my resume. It has the full pipeline - including parsing the PDF and chunking it to load it into a Chroma database.

### Agents used

There are two tools used in the project:
- a tool that does retrieval-augmented generation from the PDF file containing my resume
- a tool that allows people to leave their contact information if they want to chat with me

### Modules written

The project is modularized (as it should be):

**The app (Gradio, runs locally and on EC2)**
- **app.py** contains the Gradio web UI. Replies stream as they're generated, and the conversation is kept server-side, so visitors can't rewrite earlier turns.
- **helpers.py** contains what both versions share: the persona prompt, model settings and spend limits, the tool definitions, input sanitizing, and the streaming tool-calling loop.
- **database.py** contains the SQLite logging of conversations (deleted after a year) and contact requests (with an email check and an hourly cap).
- **rag_engine.py** contains a RAGEngine class that chunks the resume, stores it in Chroma and retrieves the most relevant chunks for a question.

**The serverless version (`serverless/`)**
- **server.py** is the chat API: a Starlette app run inside Lambda by the Lambda Web Adapter. It streams replies, keeps sessions, logs and contact requests in DynamoDB, and reloads the OpenAI key if it's rotated.
- **retriever.py** searches pre-computed resume embeddings in memory: the same interface as RAGEngine, without a vector database.
- **package.py** builds the Lambda zip: it embeds the resume when the PDF changes and installs the locked dependencies for Lambda's arm64 runtime.
- **run.sh** is the Lambda handler that starts the app under the Web Adapter.
- **web/** contains the static chat page (`index.html`, `app.js`, `style.css`).

**Infrastructure (`infra/`)**
- **serverless.yaml**, **digitaltwin.yaml** (EC2), **waf.yaml** and **pipeline.yaml** are the CloudFormation templates.
- **deploy.py** builds, uploads and deploys either version, and also stores the OpenAI key (`set-openai-key`), audits dependencies, smoke-tests the live site and bootstraps the pipeline.
- **deploy.ps1** / **deploy-serverless.ps1** are thin PowerShell wrappers around it; **destroy.ps1** / **destroy-serverless.ps1** (with **common.ps1**) tear the stacks down.
- **guard/** contains the cfn-guard security rules the pipeline enforces.

**CI/CD (`buildspecs/`)**
- **build.yml** and **publish.yml** are the CodeBuild steps of the pipeline: quality gates and packaging, then site publishing and smoke tests.

**Tests (`tests/`)**
- **test_digitaltwin.py** covers the Gradio app, the database and the RAG engine; **test_server.py** the Lambda API's key handling; **test_deploy.py** the deploy tooling (with AWS calls stubbed) and the consistency between templates and buildspecs; **test_serverless_package.py** the Lambda startup script. **conftest.py** loads modules with stubbed dependencies.

### Deploying to AWS

Two CloudFormation deployments are provided. Both default to **eu-north-1 (Stockholm)**; pass `-Region` to change it. App data never leaves the chosen EU region. The only thing created in us-east-1 is the optional WAF web ACL (`infra/waf.yaml`), because AWS requires CloudFront web ACLs to live there. It holds rules only, no data.

OpenAI usage is billed separately and isn't included in the AWS costs below. Before deploying either version, put a **budget/usage limit on the OpenAI project** whose key you use (OpenAI dashboard). It's the only control that caps OpenAI spend itself; everything below limits request rates.

**OpenAI key:** use a dedicated project key (service account, restricted to *Chat completions* and *Embeddings*). Store it once, in your own terminal:

```powershell
uv run --group deploy python infra/deploy.py set-openai-key
```

It prompts without echoing, checks the key with two tiny OpenAI calls, and stores it in the Secrets Manager secret `digitaltwin/openai-api-key`. That secret is the only place the key lives: both apps read it at runtime and the serverless build reads it to embed the resume, so no `.env` file is needed for deploying. Rotating is the same command with a new key, then revoking the old one in OpenAI. The serverless Lambda reloads the key automatically when OpenAI rejects the old one; on EC2, restart the service via SSM (`sudo systemctl restart digitaltwin`).

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
| Observability | Structured JSON logs, X-Ray tracing, and alarms for Lambda errors, Function URL 5xx, sustained throttling, and OpenAI rejecting the stored key (an expired or revoked key otherwise only shows up as generic error replies). |

In practice this version is deployed by the [CI/CD pipeline](#cicd-pipeline) on every push to `main`. The script below deploys it by hand, e.g. into another account or region:

```powershell
.\infra\deploy-serverless.ps1 -NotificationEmail you@example.com
```

When `linkedin.pdf` changes, the build re-embeds it using the key from the secret. The deploy script reserves 5 concurrent executions as a global cap. If the account's Lambda concurrency quota is too low to reserve any (new accounts can start at 10), it deploys without the cap and warns.

Cost (eu-north-1 list prices, Oct 2026): about **$10.40/month** ($1.40 with `-NoWaf`), almost all of it the WAF ($9), the KMS key ($1, only with `-NotificationEmail`) and the OpenAI secret ($0.40). Usage-based charges stay under $1/month even at 50,000 messages, thanks to Lambda's always-free tier.

#### Tearing down

```powershell
.\infra\destroy-serverless.ps1                          # serverless stack, its site bucket and WAF stack
.\infra\destroy.ps1                                     # EC2 stack and its WAF stack
.\infra\destroy.ps1 -DeleteData -DeleteShared           # ...plus the data volume, OpenAI secret and artifact bucket
```

**Remove the CI/CD pipeline first** if you've bootstrapped it; otherwise the next push to `main` redeploys the app you just destroyed. Delete the `digitaltwin-pipeline` stack (`aws cloudformation delete-stack --stack-name digitaltwin-pipeline`), then empty and delete its artifact bucket (`digitaltwin-pipeline-artifactbucket-*`), which is retained like every bucket here. The WAF stack belongs to the app, so `destroy-serverless.ps1` removes it.

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


### CI/CD pipeline

Every push to `main` tests, builds and deploys the serverless version. A run takes about 6 minutes; nothing is deployed unless every check passes. The pipeline itself is CloudFormation too (`infra/pipeline.yaml`).

```
git push ─▶ Source ─▶ Build ─────────────────────────────▶ Deploy ───────────────▶ Publish
            GitHub    pytest, cfn-lint, cfn-guard,           CloudFormation          site sync, CloudFront
            (Code-    pip-audit; build + upload the          (least-privilege        invalidation, smoke
            Connec-   Lambda zip, write params.json          deploy role)            tests (no OpenAI calls)
            tions)    [buildspecs/build.yml]                                         [buildspecs/publish.yml]
```

Commits that only touch `*.md` files or `screenshots/` don't trigger a run.

#### Quality gates

| Stage | Check | Fails the pipeline when |
|---|---|---|
| Build | `pytest` (app, Lambda server, deploy tooling, template consistency) | any test fails |
| Build | `cfn-lint` on all four templates | a template is invalid |
| Build | `cfn-guard` with `infra/guard/*.guard` | a security rule is broken: public or unencrypted buckets, unrotated keys, `Action: "*"`, internet-facing ingress, plain-HTTP CloudFront, a Function URL without IAM auth, a role without the permissions boundary, an unprotected contact table |
| Build | `pip-audit` (`deploy.py audit`) | a package **the Lambda ships** has a known vulnerability. The rest of the lockfile (Gradio/EC2 app, dev tools) is reported but doesn't block, since this pipeline doesn't deploy it. |
| Deploy | CloudFormation | the stack update fails; CloudFormation rolls back automatically |
| Publish | Smoke tests (`deploy.py smoke-test`) | the site doesn't serve this commit's `index.html` with its security headers, static assets fail, `POST /api/chat` doesn't return the app's own 400 for an empty message, or the Function URL accepts an unsigned direct call |

The Lambda package is reproducible: unchanged code produces the same content-hashed S3 key, so the Deploy stage is a no-op. Resume embeddings are cached in the artifact bucket under `linkedin.pdf`'s SHA-256, so the OpenAI embeddings API is only called when the PDF changes.

#### Security model

| Role | Can | Can't |
|---|---|---|
| Pipeline | read **only** `ILikeNNs/digitaltwinAWS` through the GitHub connection, start the two builds, deploy the app stack via the deploy role | read other repositories, touch other stacks |
| Build | upload under `lambda/` and `embeddings/` in the artifact bucket, read the OpenAI key (only needed when re-embedding) | deploy anything, change bucket settings |
| Publish | write the site bucket, invalidate CloudFront, read the app stack's outputs | change infrastructure |
| CloudFormation deploy | create, update and delete exactly the resource types in `serverless.yaml`, scoped to `digitaltwin-serverless-*` names where AWS allows | create an IAM role **without** the `WorkloadPermissionsBoundary` |

The last row is the privilege-escalation guard. A role that can create roles could otherwise create an admin role, so control of the repository would mean control of the account. Every role the pipeline creates must carry a permissions boundary that caps it at what the chatbot needs (DynamoDB, logs, X-Ray, one secret, SNS, KMS). Tests keep the boundary in sync with the app roles' permissions.

Pipeline artifacts and build logs are encrypted with a customer-managed KMS key, artifacts expire after 30 days, and tool versions are pinned (the cfn-guard binary is verified by SHA-256).

#### Bootstrap (once)

Prerequisites: the GitHub connection `digitaltwin-github` approved in the console (Developer Tools → Settings → Connections), and the OpenAI key stored with `set-openai-key`. Then:

```powershell
uv run --group deploy python infra/deploy.py pipeline --notification-email you@example.com
```

This creates or updates, in order:
1. the versioned artifact bucket and the OpenAI key secret, if missing;
2. the WAF web ACL stack in us-east-1, since CloudFront web ACLs must live there and the pipeline only passes its ARN on;
3. the pipeline stack (`digitaltwin-pipeline`), which starts its first run immediately.

Confirm the SNS subscription emails to receive pipeline-failure, alarm and contact-request emails. Changes to `infra/pipeline.yaml` aren't deployed by the pipeline itself: re-run the command above after editing it.

If the account's Lambda concurrency quota is below 105, the global cap of 5 simultaneous replies can't be reserved (Lambda keeps 100 unreserved). The bootstrap then deploys without it and warns. Request a quota increase in Service Quotas, then re-run the bootstrap.

#### Day to day

- **Deploy:** push to `main`. Pipeline failures are emailed to the notification address.
- **Watch a run:** use the `PipelineConsoleUrl` output of the `digitaltwin-pipeline` stack. Build and publish logs are in CloudWatch Logs under `/aws/codebuild/digitaltwin-pipeline-*`.
- **Roll back:** revert the commit and push (`git revert <sha> && git push`). This redeploys the previous code *and* site through every check. For an urgent rollback of the stack alone, CodePipeline can re-run the Deploy stage with an earlier successful execution's artifacts (`aws codepipeline rollback-stage`), but that leaves the site files as they are.
- **Rotate the OpenAI key:** `uv run --group deploy python infra/deploy.py set-openai-key`. No deploy is needed; the Lambda picks the new key up when OpenAI rejects the old one.

#### Troubleshooting

| Symptom | Cause and fix |
|---|---|
| Deploy fails with `... is not authorized to perform: <action>` | The deploy role lacks a permission a new or changed resource needs. Add the action to `CfnDeployRole` in `infra/pipeline.yaml` (scoped like its neighbours), re-run the bootstrap, then retry the stage. |
| Stack stuck in `ROLLBACK_FAILED` / `DELETE_FAILED` | A resource couldn't be deleted during rollback. Run `delete-stack` once (it fails again, leaving the stack in `DELETE_FAILED`), then again with `--retain-resources <LogicalId>` for the resource that failed. Remove the failed attempt's leftovers by hand: retained tables and site buckets keep their `digitaltwin-serverless-` names. Then push or retry. |
| Build fails in `pip-audit` | A dependency the Lambda ships has a published vulnerability. Update it (`uv lock --upgrade-package <name>`), commit, push. |
| Publish fails a smoke test | The message names the failing check. A new CloudFront distribution can take a few minutes to propagate, so checks retry for up to 5 minutes before failing. |

#### Cost

About **$2.00–2.50/month** on top of the app, measured on the first runs (eu-north-1 list prices): two KMS keys ($1 each; the second only with a notification email), plus a few cents of CodeBuild (~1.2 build-minutes per deploy on arm64) and CodePipeline, which is free for the first pipeline and the first 100 action-minutes a month. So the serverless version costs about **$13/month** in total, before OpenAI usage.
