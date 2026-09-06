resource "aws_ecr_repository" "app" {
  name                 = var.name
  image_tag_mutability = "MUTABLE" # CI re-points :latest; every push also gets an immutable :<sha>
  force_delete         = true      # tear-down removes the images too

  image_scanning_configuration {
    scan_on_push = true
  }
}

resource "aws_ecr_lifecycle_policy" "app" {
  repository = aws_ecr_repository.app.name

  policy = jsonencode({
    rules = [{
      rulePriority = 1
      description  = "keep the last 10 images"

      selection = {
        tagStatus   = "any"
        countType   = "imageCountMoreThan"
        countNumber = 10
      }

      action = { type = "expire" }
    }]
  })
}

# One shared /data for the api and worker tasks. The document library, memory notes,
# Chroma store and generated PDFs all live under KYRA_DATA_DIR, and the worker writes
# the PDF the api serves - so the two containers need the same filesystem. EFS gives
# the laptop's code that with no per-store object-storage adapters (a later slice).
resource "aws_efs_file_system" "data" {
  encrypted = true
  tags      = { Name = "${var.name}-data" }

  lifecycle_policy {
    transition_to_ia = "AFTER_30_DAYS"
  }
}

resource "aws_efs_mount_target" "data" {
  count           = 2
  file_system_id  = aws_efs_file_system.data.id
  subnet_id       = aws_subnet.public[count.index].id
  security_groups = [aws_security_group.efs.id]
}

resource "aws_efs_access_point" "data" {
  file_system_id = aws_efs_file_system.data.id

  posix_user {
    uid = 0
    gid = 0
  }

  root_directory {
    path = "/kyra"

    creation_info {
      owner_uid   = 0
      owner_gid   = 0
      permissions = "0755"
    }
  }
}
