output "url" {
  description = "Open this once an image has been pushed and the api task reports healthy."
  value       = local.https ? "https://${aws_lb.api.dns_name}" : "http://${aws_lb.api.dns_name}"
}

output "ecr_repository_url" {
  description = "Where CI (or a manual docker push) sends the image."
  value       = aws_ecr_repository.app.repository_url
}

output "github_deploy_role_arn" {
  description = "Set this as the AWS_ROLE_ARN repository variable in GitHub to enable the deploy job."
  value       = aws_iam_role.github_deploy.arn
}

output "anthropic_secret_name" {
  description = "Secrets Manager name to put the Anthropic API key into (README step 3)."
  value       = aws_secretsmanager_secret.anthropic.name
}

output "cluster_name" {
  value = aws_ecs_cluster.main.name
}

output "db_address" {
  value = aws_db_instance.db.address
}

output "efs_id" {
  value = aws_efs_file_system.data.id
}
