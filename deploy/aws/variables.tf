variable "region" {
  description = "AWS region for everything."
  type        = string
  default     = "us-west-2"
}

variable "name" {
  description = "Resource-name prefix; also the ECR repository and ECS cluster name (the CI deploy job assumes 'kyra')."
  type        = string
  default     = "kyra"
}

variable "allowed_cidrs" {
  description = "Who may reach the load balancer. Kyra has no login yet (auth is Phase 2), so this is the only gate between the internet and your API-key spend and personal data: your own public IP as a /32."
  type        = list(string)

  validation {
    condition     = !contains(var.allowed_cidrs, "0.0.0.0/0")
    error_message = "Kyra has no authentication yet; 0.0.0.0/0 would expose your API-key spend and personal data to the whole internet. Use your public IP as a /32 (curl -s https://checkip.amazonaws.com)."
  }
}

variable "github_repo" {
  description = "owner/name of the GitHub repository whose master branch may push images and redeploy (OIDC trust, no stored access keys)."
  type        = string
  default     = "minhducvo04/kyra"
}

variable "image_tag" {
  description = "ECR image tag the services run. CI pushes both :latest and :<git sha>."
  type        = string
  default     = "latest"
}

variable "acm_certificate_arn" {
  description = "Optional ACM certificate ARN. Set to serve HTTPS (port 80 then redirects); empty serves plain HTTP on port 80."
  type        = string
  default     = ""
}

variable "api_cpu" {
  description = "Fargate CPU units for the api task (1024 = 1 vCPU). The api process loads the BGE embedding model."
  type        = number
  default     = 512
}

variable "api_memory" {
  description = "Fargate memory (MiB) for the api task."
  type        = number
  default     = 2048
}

variable "worker_cpu" {
  description = "Fargate CPU units for the worker task (LaTeX compiles + Claude calls)."
  type        = number
  default     = 256
}

variable "worker_memory" {
  description = "Fargate memory (MiB) for the worker task."
  type        = number
  default     = 1024
}

variable "api_desired_count" {
  description = "Number of api tasks. 0 pauses the api without destroying anything."
  type        = number
  default     = 1
}

variable "worker_desired_count" {
  description = "Number of worker tasks. 0 pauses background jobs."
  type        = number
  default     = 1
}

variable "db_instance_class" {
  description = "RDS instance class. db.t4g.micro is the smallest Postgres 16 class (~$13/month)."
  type        = string
  default     = "db.t4g.micro"
}

variable "log_retention_days" {
  description = "CloudWatch log retention for the api and worker log groups."
  type        = number
  default     = 14
}
