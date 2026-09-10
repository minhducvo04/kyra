# Deploy the verified integration

Runtime source: `7da40d2`, independently reviewed and locally verified. This deployment uses the existing API/worker capacity, ECR repository, RDS database and EFS storage. Git pushes, CI variables, HTTPS and access-policy changes are outside this step.

1. Capture current service/task definitions, image digest and RDS recovery metadata. -> verify: both existing services are healthy; rollback image remains available.
2. Publish the tested local image under its source commit tag. -> verify: ECR digest equals the tested local image.
3. Run a one-shot migration with that image before rollout. -> verify: private pre-migration row backup on existing EFS, all original rows unchanged, Alembic head `d210a93e7b61`, exit code 0.
4. Move the existing latest tag and roll both services. -> verify: completed deployments, unchanged desired counts and running image digest.
5. Exercise live tool/chat, sourced suggestions, daily cache, accept/retry/dismiss, checkpoints and learning with fictional fixtures. -> verify: actual hosted responses, RDS receipts, expected conflicts and empty-source abstention. External Workspace access must remain closed without its required token.
6. Remove verification state and retire the migration task definition. Restart only the API on the same image to clear transient conversation history. -> verify: original database rows, memory IDs, routing log and notes preserved; no temporary task remains.
7. Record a final load-balancer request and its CloudWatch access event. -> verify: both services healthy on the expected digest after cleanup, with no Git push or access-rule change.
