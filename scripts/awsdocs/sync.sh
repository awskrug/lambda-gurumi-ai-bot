#!/usr/bin/env bash
set -euo pipefail

# Sync AWS official PDF documentation to S3 for Knowledge Base ingestion
# Usage: ./sync.sh <s3-bucket> [docs-file]
#
# docs.txt format: name url
#   s3 https://docs.aws.amazon.com/pdfs/AmazonS3/latest/userguide/s3-userguide.pdf
#
# PDFs over 50MB are automatically split into smaller parts using qpdf.

S3_BUCKET="${1:?Usage: sync.sh <s3-bucket> [docs-file]}"
DOCS_FILE="${2:-$(dirname "$0")/docs.txt}"
BUILD_DIR="build/awsdocs"
MAX_FILE_SIZE=$((50 * 1024 * 1024))  # 50MB Bedrock limit
SPLIT_PAGES=100                      # Pages per split chunk

rm -rf "${BUILD_DIR}"
mkdir -p "${BUILD_DIR}"

# Read docs list, skip comments and empty lines
grep -v '^\s*#' "${DOCS_FILE}" | grep -v '^\s*$' | while read -r name url; do
  echo "=== Downloading ${name} ==="
  out_dir="${BUILD_DIR}/${name}"
  mkdir -p "${out_dir}"

  curl -sL -o "${out_dir}/${name}.pdf" "${url}"
  file_size=$(stat -f%z "${out_dir}/${name}.pdf" 2>/dev/null || stat -c%s "${out_dir}/${name}.pdf" 2>/dev/null)
  size_mb=$(echo "scale=1; ${file_size}/1048576" | bc)
  echo "Downloaded ${name}.pdf (${size_mb}MB)"

  # Split large PDFs into smaller parts
  if [ "${file_size}" -gt "${MAX_FILE_SIZE}" ]; then
    echo "Splitting ${name}.pdf (exceeds 50MB limit)..."
    qpdf --split-pages="${SPLIT_PAGES}" "${out_dir}/${name}.pdf" "${out_dir}/${name}-%d.pdf"
    rm -f "${out_dir}/${name}.pdf"
    split_count=$(find "${out_dir}" -name '*.pdf' | wc -l | tr -d ' ')
    echo "Split into ${split_count} parts"
  fi

  # Sync to S3
  echo "Syncing to s3://${S3_BUCKET}/documents/${name}/"
  aws s3 sync --delete \
    "${out_dir}/" \
    "s3://${S3_BUCKET}/documents/${name}/"

  rm -rf "${out_dir}"
  echo ""
done

echo "=== All docs synced ==="
