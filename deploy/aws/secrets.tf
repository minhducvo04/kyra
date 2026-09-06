# The Anthropic key is never in Terraform state or in git: this creates an empty secret
# and you set the value once with the AWS CLI (README step 3). ECS injects it at start-up.
resource "aws_secretsmanager_secret" "anthropic" {
  name                    = "${var.name}/anthropic-api-key"
  recovery_window_in_days = 0 # tear-down deletes it at once instead of a 30-day hold
}

resource "aws_secretsmanager_secret" "database_url" {
  name                    = "${var.name}/database-url"
  recovery_window_in_days = 0
}

resource "aws_secretsmanager_secret_version" "database_url" {
  secret_id     = aws_secretsmanager_secret.database_url.id
  secret_string = local.database_url
}
