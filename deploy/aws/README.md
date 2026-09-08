# Kyra on AWS (v2 Phase 1, slice 4)

Terraform for the same shape `docker-compose.yml` runs locally: an **api** service and a **worker** service on
ECS Fargate from one image, **RDS Postgres** behind `DATABASE_URL`, one shared **EFS** volume mounted at `/data`,
an **ALB** in front, secrets in **Secrets Manager**, logs in **CloudWatch**, images in **ECR**, and a GitHub
Actions deploy that authenticates with **OIDC** (no stored AWS keys). Nothing here runs on the laptop; local use
is unchanged.

Status: written and validated in CI (`terraform validate`), **never applied** - the first `apply` is yours,
against an account and a spend ceiling you've chosen.

## Cost (us-west-2, on-demand, approximate)

| Resource | Monthly while running |
|---|---|
| Fargate api task (0.5 vCPU, 2 GB) | ~$34 |
| Fargate worker task (0.25 vCPU, 1 GB) | ~$17 |
| RDS `db.t4g.micro`, 20 GB gp3 | ~$13 |
| Application Load Balancer | ~$17 + traffic |
| EFS, ECR, Secrets Manager, CloudWatch | < $3 |
| **Total** | **~$85** |

Pausing (`api_desired_count = 0`, `worker_desired_count = 0`) drops it to ~$33 (RDS + ALB + storage).
`./teardown.sh` drops it to ~$0. The AWS Free Tier covers part of RDS/ALB for a new account's first year.
No NAT gateway on purpose: tasks get public IPs and only the ALB may reach their port.

## Prerequisites

- An AWS account with an IAM user/role that can create the above, and the AWS CLI logged in (`aws sts get-caller-identity`).
- Terraform >= 1.6: `brew install hashicorp/tap/terraform`.
- Your public IP: `curl -s https://checkip.amazonaws.com`.

## Steps

1. **Variables**: `cp terraform.tfvars.example terraform.tfvars`, set `allowed_cidrs` to your IP as a `/32`.
   Kyra has no login yet, so this list is the only gate between the internet and your API-key spend and
   personal data; `0.0.0.0/0` is rejected by a validation rule.
2. **Create the stack**: `terraform init`, `terraform plan`, `terraform apply`. The ECS services will sit at
   0 healthy tasks until an image exists in ECR - expected.
3. **Put the Anthropic key in Secrets Manager** (the value never touches Terraform state or git):
   ```bash
   aws secretsmanager put-secret-value --secret-id kyra/anthropic-api-key --secret-string "$(grep '^ANTHROPIC_API_KEY=' ../../.env | cut -d= -f2-)"
   ```
4. **Enable the CI deploy**: in the GitHub repo, Settings -> Secrets and variables -> Actions -> **Variables**,
   add `AWS_ROLE_ARN` = the `github_deploy_role_arn` output and `AWS_REGION` = your region. The `deploy` job in
   `.github/workflows/ci.yml` is skipped while those are unset; once set, every push to `master` builds the
   image, pushes `:latest` and `:<sha>` to ECR, and forces a new deployment of both services.
   Manual alternative: `aws ecr get-login-password | docker login ...`, `docker build -t <ecr url>:latest .`, push.
5. **Migrations** (the app also runs `create_all` at start, so a fresh database works without this; the standard
   is still an explicit migration step):
   ```bash
   aws ecs run-task --cluster kyra --launch-type FARGATE --platform-version 1.4.0 \
     --task-definition kyra-worker \
     --network-configuration "awsvpcConfiguration={subnets=[<public subnet id>],securityGroups=[<tasks sg id>],assignPublicIp=ENABLED}" \
     --overrides '{"containerOverrides":[{"name":"worker","command":["alembic","upgrade","head"]}]}'
   ```
6. **Open** the `url` output. Logs: CloudWatch `/kyra/api` and `/kyra/worker`. A shell inside a task:
   `aws ecs execute-command --cluster kyra --task <id> --container api --interactive --command /bin/bash`.
7. **Pause or destroy**: set the desired counts to 0 and `terraform apply`, or `./teardown.sh`.

## What is deliberately not here (and why)

- **SQS**: the `jobs` table on RDS already gives durable state and streamable progress, and the SSE endpoint
  reads progress from that table anyway; SQS would only replace the worker's 1-second claim poll. It goes in
  behind the existing `JobQueue` interface when a second worker or a real backlog exists.
- **S3 for PDFs**: the worker writes a PDF the api serves, but so do the document library, memory notes and
  Chroma store - all under `KYRA_DATA_DIR`. One EFS mount solves all of them with zero code change; an
  object-storage adapter per store is the later "JobDocumentStore to S3" slice.
- **Real auth (accounts, OIDC)**: Phase 2. Since 2026-09-08 there IS a login - `KYRA_API_TOKEN` plus a
  signed session cookie, so a browser can authenticate from anywhere - but it is one token for one tenant,
  not users. Set `KYRA_TRUST_LOOPBACK=false` on any task behind the ALB. The CIDR allow-list is still worth
  keeping as a second layer; widening it now costs a lot less than it used to, but it is not free.
- **Remote Terraform state**: local state is fine for one operator; the S3 backend block is commented in
  `providers.tf` for when a second person runs this.
- **Immutable image tags in the task definition**: the services run `:latest` and CI forces a new deployment;
  pinning `:<sha>` per deploy (and rolling back by tag) is the stricter standard and a small follow-up.
- **The voice stack and local models**: Apple-Silicon/laptop concerns (see `docs/v2-outline.md`); in the
  container the router falls back to Claude.
