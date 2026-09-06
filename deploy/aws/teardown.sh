#!/usr/bin/env bash
# Destroys every AWS resource Terraform created for Kyra. RDS goes without a final snapshot
# and the EFS volume (cloud copies of the document library / memory notes / PDFs) goes with it.
# The laptop's data/ directory is untouched; local use keeps working exactly as before.
set -euo pipefail
cd "$(dirname "$0")"
echo "This deletes the Kyra stack in AWS: ECS services, the RDS database (no snapshot), the EFS volume,"
echo "ECR images, both secrets, the load balancer and the VPC. Monthly spend goes to ~\$0 afterwards."
read -r -p "Type 'destroy' to continue: " answer
if [ "$answer" != "destroy" ]; then
  echo "aborted - nothing changed"
  exit 1
fi
terraform destroy
