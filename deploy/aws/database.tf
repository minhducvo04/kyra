resource "random_password" "db" {
  length  = 32
  special = false # keeps the URL free of characters that would need escaping
}

resource "aws_db_subnet_group" "db" {
  name       = "${var.name}-db"
  subnet_ids = aws_subnet.private[*].id
}

resource "aws_db_instance" "db" {
  identifier              = "${var.name}-db"
  engine                  = "postgres"
  engine_version          = "16"
  instance_class          = var.db_instance_class
  allocated_storage       = 20
  storage_type            = "gp3"
  storage_encrypted       = true
  db_name                 = "kyra"
  username                = "kyra"
  password                = random_password.db.result
  db_subnet_group_name    = aws_db_subnet_group.db.name
  vpc_security_group_ids  = [aws_security_group.db.id]
  publicly_accessible     = false
  backup_retention_period = 1
  apply_immediately       = true

  # Single-user project: tear-down must be one command, so no final snapshot and no deletion protection.
  skip_final_snapshot = true
  deletion_protection = false
}

locals {
  database_url = "postgresql+psycopg://kyra:${random_password.db.result}@${aws_db_instance.db.address}:5432/kyra"
}
