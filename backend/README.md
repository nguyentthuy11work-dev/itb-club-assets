# AMS Backend

> Cách đơn giản nhất để chạy toàn bộ web là dùng một lệnh tại thư mục gốc:
> `docker compose up --build -d`. Xem hướng dẫn đầy đủ trong `../README.md`.

Hạ tầng local của backend dùng PostgreSQL 18 và Flyway. Docker Compose chỉ
quản lý database và migrations; ứng dụng Python được chạy riêng trong môi
trường phát triển.

## Yêu cầu

- Docker Engine và Docker Compose v2 (`docker compose version`)
- (Tuỳ chọn) DBeaver để xem database

## Khởi tạo cấu hình

Tạo `backend/.env`. File này chứa secret local, đã được gitignore và không
được commit.

```dotenv
POSTGRES_DB=ams
POSTGRES_USER=ams_app
POSTGRES_PASSWORD=MotMatKhauLocalDai_2026
POSTGRES_PORT=5432

AMS_DATABASE_URL=postgresql+asyncpg://ams_app:MotMatKhauLocalDai_2026@localhost:5432/ams
AMS_JWT_SECRET=<secret-ngau-nhien-toi-thieu-32-byte>
AMS_JWT_ISSUER=ams-api
AMS_JWT_AUDIENCE=ams-web
AMS_ACCESS_TOKEN_MINUTES=15
AMS_REFRESH_TOKEN_DAYS=7
AMS_REFRESH_COOKIE_SECURE=false
AMS_CORS_ALLOWED_ORIGINS=["http://localhost:5173"]
AMS_LOG_LEVEL=INFO
```

`POSTGRES_PASSWORD` và password trong `AMS_DATABASE_URL` phải giống nhau.
Để tránh phải URL-encode, password local nên chỉ dùng chữ, số, `_` hoặc `-`.
`AMS_CORS_ALLOWED_ORIGINS` là JSON array gồm các origin cụ thể; không dùng `*`
vì API cho phép credential cookie. Có thể dùng `[]` khi frontend đi qua Vite proxy
cùng origin.

Có thể tạo JWT secret:

```bash
python -c 'import secrets; print(secrets.token_urlsafe(48))'
```

## Chạy PostgreSQL

Từ thư mục này:

```bash
docker compose up -d postgres
docker compose ps
```

PostgreSQL sẵn sàng khi trạng thái là `healthy`. Xem log khi cần:

```bash
docker compose logs --tail=100 postgres
```

Dừng các service nhưng giữ lại dữ liệu:

```bash
docker compose down
```

## Chạy migrations

Flyway là một service thuộc profile `tools`; chạy migration hiện có bằng:

```bash
docker compose --profile tools run --rm flyway
```

Các thay đổi schema phải được thêm bằng migration versioned mới trong
`migrations/` (ví dụ `V2__add_assets.sql`), không tạo/sửa bảng thủ công trong
DBeaver.

## Chạy và kiểm tra API

Khởi động API từ thư mục `backend/`:

```bash
uv run --env-file .env uvicorn ams.interface.api.main:create_app --factory --reload --port 8000
```

- `GET /health` kiểm tra process còn sống và không truy cập database.
- `GET /ready` chạy probe `SELECT 1` tới PostgreSQL; dependency lỗi trả `503`
  theo error envelope chuẩn.
- Mọi response có `X-Trace-ID`; mọi error trả cùng ID trong `traceId`.
- Log request được xuất dạng JSON với trace, route, status và duration; request đã
  xác thực có thêm actor ID.

## Kết nối bằng DBeaver

Điền các giá trị từ `.env`:

| Trường | Giá trị |
| --- | --- |
| Database | `ams` (`POSTGRES_DB`) |
| User | `ams_app` (`POSTGRES_USER`) |
| Password | `POSTGRES_PASSWORD` |
| Port | `5432` (`POSTGRES_PORT`) |

Nếu Docker chạy native trong WSL còn DBeaver chạy trên Windows, `localhost`
có thể trỏ sang một PostgreSQL khác trên Windows. Khi đó dùng IP của WSL làm
Host:

```bash
hostname -I
```

Lấy địa chỉ IPv4 đầu tiên (ví dụ `172.28.131.102`) và dùng nó trong DBeaver.
IP này có thể thay đổi sau khi WSL khởi động lại. Dấu hiệu kết nối đúng là
DBeaver hiển thị PostgreSQL 18, phù hợp với image Compose.

## Khắc phục sự cố

`FATAL: password authentication failed for user "ams_app"`:

1. Xác nhận host/port đang trỏ đúng PostgreSQL container. Ngay sau khi test
   connection, chạy `docker compose logs --tail=50 postgres`; không có log từ
   lần test thường nghĩa là client đang kết nối một instance khác.
2. Kiểm tra `POSTGRES_PASSWORD` được nhập chính xác; không dùng toàn bộ
   `AMS_DATABASE_URL` làm password.
3. DBeaver có thể lưu password cũ. Xoá password đã lưu hoặc tạo connection
   mới.

Các biến `POSTGRES_DB`, `POSTGRES_USER` và `POSTGRES_PASSWORD` chỉ được áp
dụng khi volume PostgreSQL được khởi tạo lần đầu. Đổi chúng sau đó không thay
đổi database/user hiện hữu. Với dữ liệu local có thể bỏ được, hãy chủ động xác
nhận trước khi xoá volume rồi dùng:

```bash
docker compose down -v
docker compose up -d postgres
```

Lệnh `down -v` xoá toàn bộ dữ liệu database local.
