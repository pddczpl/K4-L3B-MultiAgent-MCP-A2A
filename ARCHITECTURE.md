# L3B Architecture Record

Team phải cập nhật tài liệu này cùng source. Mục tiêu là mô tả quyết định có thể kiểm chứng, không ghi prompt bí mật hoặc chain-of-thought.

## 1. System overview

Vẽ hoặc mô tả luồng từ input/candidate resolution đến MCP investigation, specialist agents, conflict resolver, verifier, output và trace.

```text
Input → Entity Resolver → Coordinator → Specialists → Conflict Resolver → Verifier → Output
            │                              │                  │             │
            └──────────────────────────── MCP ────────────────┴──────────── Trace
```

## 2. Agent ownership

| Actor | Input | Trách nhiệm | Tool permission | Output/handoff |
| --- | --- | --- | --- | --- |
| Entity/customer | Candidate list, claim | Xác thực và tìm đúng customer/order ID | `get_customer_history`, `get_order_details` | Resolved IDs -> Coordinator |
| Coordinator | Resolved IDs, Claim | Lên kế hoạch, điều phối Specialists, tổng hợp kết quả | (Không cấp tool, chỉ quản lý) | Context -> Specialists, Claims -> Conflict resolver |
| Order/product | order_id | Trích xuất thông tin sản phẩm, đơn giá | `get_order_details`, `get_product_info` | Order evidence -> Coordinator |
| Shipment | tracking_id | Kiểm tra trạng thái vận chuyển | `get_shipment_status` | Shipment evidence -> Coordinator |
| Payment/refund | order_id | Tra cứu lịch sử thanh toán, hoàn tiền | `get_payment_status`, `get_refund_status` | Payment evidence -> Coordinator |
| Policy | Case context | Truy xuất điều khoản chính sách của shop | `get_shop_policy` | Policy rules -> Conflict resolver |
| Conflict resolver | Evidences, Policies | Phân tích xung đột giữa các nguồn dữ liệu | (Không cấp tool) | Resolved claim, Confidence -> Verifier |
| Verifier | Draft Output, All evidence | Kiểm chứng schema, logic và invariants | (Không cấp tool) | Final JSON Output |

Áp dụng least privilege; tool discovery không đồng nghĩa mọi actor đều được gọi mọi tool.

## 3. Entity resolution và A2A protocol

- **Entity resolution:** So sánh đặc trưng (tên, email, ngày tháng) giữa candidate và evidence từ MCP. Hệ thống chọn candidate có điểm tương đồng cao nhất. Confidence threshold = 0.8. Nếu không có candidate nào đạt, đánh dấu ambiguous.
- **A2A Message envelope:** Việc giao việc và bàn giao được ghi bằng các event
  `task_assigned` và `handoff`, luôn tương quan bằng `case_id`.
- **Handoff & tránh vòng lặp:** Workflow có hai pha điều tra hữu hạn (entity và
  specialist), sau đó synthesize và verify; không có vòng hội thoại không giới hạn.

## 4. Evidence và conflict lifecycle

- **Validation & Mapping:** Khi MCP trả về, kiểm tra schema. Lưu trực tiếp chuỗi `evidence_ref` vào context, map 1-1 với fact được rút trích.
- **Conflict Lifecycle:** Khi nguồn dữ liệu mâu thuẫn (VD: Khách báo chưa nhận, Shipment báo đã giao), hệ thống ưu tiên nguồn log hệ thống. Mâu thuẫn không thể giải quyết sẽ giảm `confidence` và được ghi nhận vào `unresolved_conflict`.
- **Emit Trace:** Luôn gọi `trace.emit(event_type="tool_result_consumed", evidence_refs=[...])` ngay khi dữ liệu được sử dụng.
- **Caching:** Dữ liệu evidence được lưu cache in-memory theo key `case_id` để ngăn tình trạng dùng sai chéo case.

## 5. Failure and efficiency policy

| Failure | Retry budget | Fallback | Trace event/code |
| --- | ---: | --- | --- |
| MCP timeout | 0 | Bỏ call lỗi, dùng evidence còn lại | Không tạo event ngoài contract |
| Entity not found/ambiguous | 0 | Trả về confidence thấp | `handoff` |
| Source conflict | 0 | Ghi `data_conflicts`, hạ confidence | `handoff` |
| Invalid specialist result | 0 | Bỏ qua result, coi như thiếu data | `handoff` |

- **Efficiency & Cache strategy:** Sử dụng Dictionary in-memory cache cho mỗi phiên `case_id`. Key format: `hash(tool_name + args)`.
- **Query budget:** Tối đa 5 calls cho một tool và 12 calls tổng cộng mỗi case.
  Hết budget dùng dữ liệu đã có thay vì đoán.

## 6. Verification invariants

Các invariant bắt buộc kiểm tra trước khi Verifier chốt output:
1. Output tuân thủ nghiêm ngặt JSON schema quy định.
2. `evidence_ref` trong final output bắt buộc tồn tại trong cache của `case_id` đang xử lý (không vi phạm cross-case).
3. Logic mốc thời gian phải hợp lý (VD: Order date < Shipment date < Claim date).
4. Khớp giữa nguyên nhân (claim_reason), kết luận trách nhiệm (responsibility) và hành động (action).

## 7. Reproducibility

- **Primary model:** `Qwen3.5-9B` cho planning, coordinator và synthesis.
- **Verifier model:** `Qwen3-8B` độc lập để kiểm tra schema, provenance và consistency.
- **Model serving:** Ollama/vLLM qua OpenAI-compatible API; cả hai model dưới 10B.
- **Concurrency limit:** CLI xử lý tuần tự để tránh quá tải hai model local.
- **Random seed:** Cố định `seed = 42` cho các xử lý probabilistic.
- **Environment:** Yêu cầu chạy trên Python 3.11+, khóa version các thư viện trong `pyproject.toml`. Không đính kèm API Key trong source code.
