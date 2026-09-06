terraform {
  required_version = ">= 1.6"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.60"
    }
    random = {
      source  = "hashicorp/random"
      version = "~> 3.6"
    }
  }

  # State stays local (gitignored) for a single operator: it contains the RDS password.
  # Before a second person runs this, move it to an encrypted S3 backend:
  # backend "s3" { bucket = "<state-bucket>"  key = "kyra/terraform.tfstate"  region = "<region>"  encrypt = true }
}

provider "aws" {
  region = var.region

  default_tags {
    tags = { Project = "kyra", ManagedBy = "terraform" }
  }
}
