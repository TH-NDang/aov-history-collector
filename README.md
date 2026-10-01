# Bộ thu thập lịch sử đội tuyển và tuyển thủ Liên Quân / AoV

Công cụ thu thập **toàn bộ đội tuyển, tuyển thủ, huấn luyện viên/staff, lịch sử thành viên, khu vực và ảnh** từ Liquipedia Honor of Kings/Arena of Valor; xuất đồng thời JSON, CSV và thư mục ảnh.

## Dữ liệu đầu ra

```text
output/
├── json/
│   ├── teams.json
│   ├── people.json
│   ├── players.json
│   ├── staff.json
│   ├── memberships.json
│   └── regions.json
├── csv/                    # cùng dữ liệu ở dạng CSV UTF-8 BOM
├── images/
│   ├── teams/              # logo đội
│   ├── people/             # ảnh gốc theo người
│   ├── people-by-team/     # ten-doi-ten-tuyen-thu.ext
│   └── regions/            # cờ/ảnh khu vực
├── image-manifest.json     # URL nguồn, SHA-256, kích thước
├── manifest.json
└── state.json              # checkpoint
```

`memberships` là bảng quan trọng nhất để giữ lịch sử: một người có thể thuộc nhiều đội, nhiều giai đoạn và nhiều vai trò. `member_type` phân biệt `player`, `staff` và `member`; `status` phân biệt `active`/`former`; ngày vào/ra đội được giữ riêng.

## Quy tắc tên ảnh

- Logo: `images/teams/ten-doi.ext`
- Ảnh người chuẩn: `images/people/ten-hieu.ext`
- Bản theo đội: `images/people-by-team/ten-doi-ten-hieu.ext`
- Khu vực: `images/regions/ten-khu-vuc.ext`

Tên được chuẩn hóa không dấu, chữ thường, dấu cách thành dấu gạch ngang. Nếu cùng một người từng ở nhiều đội, ảnh được tạo riêng theo từng cặp đội–người đúng yêu cầu và vẫn có bản chuẩn để tránh mất liên kết.

## Chạy thử trên máy

1. Cài Python 3.12.
2. Chạy:

```bash
python -m pip install -r requirements.txt
python -m unittest discover -s tests -v
python collector.py --limit-teams 3 --limit-people 10
```

Sau khi kiểm tra ổn, chạy toàn bộ:

```bash
python collector.py
python package_output.py
```

## Chạy bằng GitHub Actions thông thường

Workflow nằm tại `.github/workflows/collect.yml`. Vào **Actions → Collect AoV historical teams and people → Run workflow** và giữ mặc định `limit_teams=80`, `limit_people=80`, không chọn `skip_images`.

Workflow dùng runner GitHub `ubuntu-latest`; không cần self-hosted runner và không cần máy cá nhân bật.

Do GitHub-hosted runner giới hạn một job tối đa 6 giờ, bộ đầy đủ được chia thành nhiều lượt. Chỉ cần khởi động lượt đầu với `auto_continue=true`. Sau mỗi lượt, workflow tự khôi phục `.cache`, dữ liệu và `output/state.json`, bỏ qua mục đã xong, nối kết quả mới rồi tự gọi lượt kế tiếp. Chuỗi tự dừng khi toàn bộ đội và người đã hoàn thành; nếu một lượt lỗi, chuỗi cũng dừng để tránh lặp lỗi.

Lặp lại tới khi file `output/state.json` trong artifact có:

```json
"finished_team_pages": bằng "known_team_pages",
"finished_people": bằng "known_people"
```

Không đặt cả hai giới hạn thành `0` khi thu thập toàn bộ trên GitHub-hosted runner vì job có thể vượt 6 giờ. Có thể giảm xuống 40–60 nếu nguồn phản hồi chậm.

## Giới hạn tốc độ và bản quyền

- Mặc định: tối thiểu 2,1 giây giữa HTTP request thông thường; 30,5 giây cho `action=parse`; tối đa 58 API request/giờ.
- Không chạy nhiều job song song để né giới hạn.
- Dữ liệu văn bản từ Liquipedia cần ghi công theo CC BY-SA.
- Ảnh có thể mang giấy phép riêng; `image-manifest.json` giữ URL nguồn và checksum để kiểm tra.
- `source_url`, `source_revision`, `collected_at` được giữ trong dữ liệu để đối chiếu.

## Lưu ý về phạm vi

Wiki hiện gộp Honor of Kings và Arena of Valor. Cấu hình mặc định `games: ["Arena of Valor"]` chỉ nhận trang đội có liên kết game AoV và chỉ tải hồ sơ những người xuất hiện trong lịch sử đội hình của các đội đó. Không dùng quốc gia hoặc tên đội để suy đoán game.

Có thể kiểm tra riêng một đội bằng:

```bash
python collector.py --team-title "Box Gaming" --person-title "Adonis"
```
