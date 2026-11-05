# Private memo attachment deployment

Memo attachment metadata lives in MySQL and is synchronized with the Todo data.
Object bodies live only in one shared private S3-compatible bucket. Browser
responses never contain the bucket, object key, endpoint or credentials.

## Runtime configuration

Attachment endpoints return HTTP 503 while
`TODO_ATTACHMENT_STORAGE_ENABLED=false`; the rest of the API starts and works
normally. Configure these values independently in every deployed API process:

```dotenv
TODO_ATTACHMENT_STORAGE_ENABLED=true
STORAGE_BACKEND=r2
S3_ENDPOINT_URL=https://<CLOUDFLARE_ACCOUNT_ID>.r2.cloudflarestorage.com
S3_BUCKET_NAME=teddy-todo-prod-attachments
S3_REGION=auto
AWS_ACCESS_KEY_ID=<R2_ACCESS_KEY_ID>
AWS_SECRET_ACCESS_KEY=<R2_SECRET_ACCESS_KEY>
TODO_ATTACHMENT_MAX_FILE_BYTES=52428800
TODO_ATTACHMENT_MAX_COUNT_PER_MEMO=100
TODO_ATTACHMENT_MAX_ZIP_BYTES=209715200
TODO_ATTACHMENT_SPOOL_BYTES=8388608
TODO_ATTACHMENT_STORAGE_TIMEOUT_SECONDS=30
TODO_ATTACHMENT_UPLOAD_STALE_SECONDS=3600
```

- Keep the R2 bucket private. Do not configure a public base URL or browser CORS.
- 기존 Travel private R2 bucket을 그대로 사용하고 `todo/` prefix로 분리해도 된다.
  새 유료 bucket을 만들 필요는 없으며, 두 Todo sync node에 동일한 bucket 설정을 넣는다.
- Give the credential object read/write access only to this bucket.
- All Todo nodes that exchange attachment metadata must use the same bucket and
  the server-calculated `todo/memos/{originMemoId}/{attachmentId}` namespace.
- Metadata keeps an immutable `origin_memo_id`. If private backlog convergence
  reparents metadata to a different current `memo_id`, its object key continues
  to use the origin ID; no object copy or delete is required.
- Do not put these values in `todo-web-next` or any `NEXT_PUBLIC_*` variable.
- R2 무료 사용량은 Cloudflare 계정 전체에서 합산되며 무제한이 아니다. 현재
  한도와 초과 요금은 배포 전에 [Cloudflare R2 pricing](https://developers.cloudflare.com/r2/pricing/)
  에서 확인한다.
- The production gateway must accept at least 50 MiB request bodies. The root
  Todo gateway contract is `client_max_body_size 64m`; verify the NAS copy too.
- Apply the API schema/sync-v4 release to both peers before enabling uploads.
  Older peers fail closed rather than treating private attachment metadata as a
  schema-drift subset.
- `uploading` and `ready` rows reserve the per-memo quota; `failed` rows do not.
  A retry must use the same UUID and exact metadata/hash. Upload reservations
  older than `TODO_ATTACHMENT_UPLOAD_STALE_SECONDS` are opportunistically
  changed to `failed` during the next reservation, bounding abandoned slots
  without a background janitor.

## Local verification

LocalStack can be used with loopback-only test credentials:

```dotenv
TODO_ATTACHMENT_STORAGE_ENABLED=true
STORAGE_BACKEND=localstack
S3_ENDPOINT_URL=http://127.0.0.1:4566
S3_BUCKET_NAME=teddy-todo-attachments
S3_REGION=us-east-1
AWS_ACCESS_KEY_ID=test
AWS_SECRET_ACCESS_KEY=test
```

The automated integration test additionally requires explicit
`TODO_ATTACHMENT_TEST_S3_*` values and refuses a non-loopback endpoint. It
uploads a random test key, verifies HEAD/GET bytes and hash, then deletes it.
Tests must never target the production R2 endpoint.

기존 DB/auth 환경값과 위 로컬 storage 값을 API `.env`에 설정한 뒤 native 실행한다.
`TODO_MODE` 프리셋이 Docker DNS를 사용한다면 호스트 실행용 `DB_HOST`, `DB_PORT`,
`SYNC_PEER_URL` 등을 명시적으로 덮어쓴다. 개발 페어와 운영 노드의 버킷을 섞지 않는다.

```bash
# todo-api-fastapi/
venv/bin/uvicorn src.__main__:app --host 127.0.0.1 --port 8000
# todo-web-next/ (별도 터미널)
NEXT_PUBLIC_API_URL=http://127.0.0.1:8000/api NEXT_PUBLIC_SOCKET_URL=http://127.0.0.1:8000 npm run dev
```

이미 실행 중인 Docker 스택을 갱신하거나 NAS에 배포하는 명령은 이번 검증에서 실행하지 않았다.

현재 자동 검증은 private LocalStack S3 fixture로 완료했다. 실제 R2 연결은 운영
자격증명이 로컬에 없으므로 미검증이며, NAS에 환경값을 넣은 뒤 별도 smoke 확인이 필요하다.
