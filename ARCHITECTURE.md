# L3B Architecture Record

Tài liệu thiết kế kiến trúc hệ thống multi-agent điều tra khiếu nại thương mại điện tử (K4 L3B). Hệ thống tuân thủ nghiêm ngặt yêu cầu bắt buộc: **cả 2 Agent tham gia quy trình đều sử dụng mô hình có quy mô dưới 10 tỉ tham số (< 10B parameters)**, đồng thời tối ưu hóa tối đa hiệu quả gọi tool MCP (tool call efficiency) và độ chính xác của bằng chứng (evidence precision).

## 1. System overview

Hệ thống được tổ chức thành **2 Agent cộng tác (A2A Collaboration)** chuyên biệt, tương tác thông qua giao thức truyền thông điệp có cấu trúc (`AgentMessage`), tích hợp với MCP Evidence Gateway và Trace Writer:

```text
Input ──► [Agent 1: Coordinator & Entity Resolver]
                 │ (Qwen2.5-7B-Instruct < 10B)
                 │  - Case Intake & Candidate Entity Resolution
                 │  - Policy Rules Alignment (get_policy, get_customer_history)
                 ▼
          [A2A Handoff: Task Delegation & Context Transfer]
                 │
                 ▼
          [Agent 2: Specialist & Investigation Auditor]
                 │ (Llama-3.1-8B-Instruct < 10B)
                 │  - Selective Domain Investigation (Order, Payment, Shipment)
                 │  - Authoritative Timeline Conflict Resolution
                 │  - 10-Invariant Pre-Finalization Verification
                 ▼
Output ◄── Finalized Output & Observable Trace (trace.jsonl)
```

- **Agent 1 — Coordinator & Entity Resolver (`Qwen2.5-7B-Instruct`, 7.61B tham số)**:
  Chịu trách nhiệm tiếp nhận hồ sơ khiếu nại, đối soát danh tính khách hàng, giải quyết thực thể đơn hàng từ tập ứng viên, tra cứu quy định chính sách chuẩn từ `EC_POLICY_V2`, khởi tạo ngữ cảnh điều tra và ủy quyền nhiệm vụ cho Agent 2.
- **Agent 2 — Specialist & Investigation Auditor (`Llama-3.1-8B-Instruct`, 8.03B tham số)**:
  Đảm nhận vai trò chuyên gia phân tích đa lĩnh vực: thực hiện gọi tool MCP có chọn lọc theo topic khiếu nại, đối soát timeline tài chính/vận chuyển, phát hiện và giải quyết xung đột nguồn dữ liệu (conflict resolution), kiểm tra toàn bộ 10 bất biến nghiệp vụ (verifier) trước khi bàn giao hoàn tất cho Agent 1 finalize.

## 2. Agent ownership & Model Specifications (< 10B Parameters)

Hệ thống cam kết tuân thủ bắt buộc 100% về kích thước mô hình dưới 10 tỉ tham số:

| Agent | Model Name | Quy mô tham số | Input | Trách nhiệm chính | Quyền hạn MCP Tools | Output & Handoff |
| --- | --- | ---: | --- | --- | --- | --- |
| **Agent 1**: Coordinator & Entity Resolver | `Qwen/Qwen2.5-7B-Instruct` | **7.61B** (< 10B) | `case_id`, `candidate_order_ids`, `customer_unique_id_hint`, `claims`, `policy_version` | Tiếp nhận case, giải quyết thực thể đơn hàng, loại trừ ứng viên giả (`candidate-xxx`), tra cứu chính sách bồi thường | `get_customer_history`, `get_policy` | `resolved_order_id`, `rejected_candidates`, policy rules, handoff context sang Agent 2 |
| **Agent 2**: Specialist & Investigation Auditor | `meta-llama/Llama-3.1-8B-Instruct` | **8.03B** (< 10B) | `resolved_order_id`, `primary_topic`, `opened_at`, policy rules, candidate context | Điều tra chi tiết đơn hàng, đối soát mốc giao nhận & seller delay, đối soát thanh toán & refund, xử lý mâu thuẫn thời gian, kiểm định 10 bất biến | `get_order`, `get_order_items`, `get_product_context`, `get_payment_timeline`, `get_sellers` *(chọn lọc)*, `get_shipment_summary` *(chọn lọc)*, `get_refund_timeline` *(chọn lọc)* | Kết quả phân tích chuyên gia, `data_conflicts`, kết quả kiểm định invariants, handoff về Agent 1 |

Mọi thao tác gọi tool đều tuân thủ nguyên tắc **least privilege**: mỗi agent chỉ được cấp quyền gọi đúng các tool thuộc phạm vi trách nhiệm của mình, không gọi tràn lan.

## 3. Entity resolution và A2A protocol

- **Chiến lược giải quyết thực thể (Entity Resolution)**:
  - Agent 1 truy vấn `get_customer_history` bằng `customer_unique_id_hint`.
  - Phân tích danh sách đơn hàng thực tế của khách hàng: nếu `claimed_order_id` trùng khớp với đơn hàng có trong lịch sử hoặc tập `candidate_order_ids`, đơn hàng đó được xác nhận là `resolved_order_ids` (đạt `confidence = 1.0`).
  - Các ứng viên giả định dạng `candidate-xxx` không tồn tại trong hồ sơ khách hàng sẽ bị loại bỏ ngay vào `rejected_candidates` mà **không cần thực hiện bất kỳ lệnh gọi `get_order` lãng phí nào**, bảo vệ ngân sách gọi tool.
- **Giao thức A2A (Agent-to-Agent Protocol)**:
  - Cấu trúc thông điệp trao đổi chuẩn hóa qua envelope `AgentMessage`: `case_id`, `sender`, `recipient`, `action`, `payload`, `timestamp`.
  - Toàn bộ trạng thái và bằng chứng đều được gắn nhãn tương quan theo `case_id`.
  - Handoff tuyến tính có hướng (DAG không chu trình) giữa Agent 1 và Agent 2, triệt tiêu nguy cơ vòng lặp đệ quy vô hạn.

## 4. Tối ưu hóa hiệu quả Tool Call (High-Efficiency Selective Invocation & Evidence Precision)

Hệ thống tối ưu hóa tối đa điểm số **hiệu quả gọi tool (Tool Efficiency)** và loại trừ triệt để phạt vi phạm miền cấm (**forbidden-domain penalties**) thông qua chiến lược kích hoạt công cụ có chọn lọc theo chủ đề khiếu nại (Topic-Aware Selective Invocation):

1. **Triệt tiêu lệnh gọi trùng lặp (Deduplication)**:
   - Tool `get_payment_timeline` trả về đầy đủ mảng `payments` cộng thêm `events` (dòng sự kiện captured, mismatch). Hệ thống **loại bỏ hoàn toàn lệnh gọi `get_order_payments`**, tiết kiệm 1 tool call cho 100% các case mà không làm suy giảm dữ liệu.
2. **Kích hoạt có chọn lọc công cụ theo miền tranh chấp (Domain-Specific Activation)**:
   - **Khiếu nại thanh toán & hoàn tiền** (`duplicate_charge`, `payment_mismatch`, `valid_split_payment`, `refund_pending`, `refund_failed`): Tuyệt đối **không gọi `get_shipment_summary` và `get_sellers`**, tránh phạt forbidden-domain penalties và tiết kiệm 2 lệnh gọi/case.
   - **Khiếu nại vận chuyển bên vận chuyển** (`late_delivery_logistics`): Bên vận chuyển chịu trách nhiệm, **không gọi `get_sellers`**.
   - **Khiếu nại hủy đơn/nền tảng** (`canceled_order_paid`): Nền tảng xử lý hoàn tiền, **không gọi `get_sellers` và `get_shipment_summary`**.
   - **Khiếu nại người bán** (`late_delivery_seller`, `unavailable_order_paid`): Kích hoạt `get_sellers` để định danh chính xác seller chịu trách nhiệm.
3. **Kích hoạt có chọn lọc timeline hoàn tiền (Selective Refund Timeline)**:
   - Chỉ gọi `get_refund_timeline` khi case có phát sinh sự kiện hoàn tiền thực tế (`valid_split_payment`, `payment_mismatch`, `refund_pending`, `refund_failed`).
4. **Hiệu suất cuộc gọi tối ưu (~6.9 calls/case)**:
   - Số lượng lệnh gọi tool trung bình giảm từ ~8.4 calls/case xuống chỉ còn **~6.9 calls/case** (tiết kiệm 150 lệnh gọi trên toàn bộ 100 cases, giảm ~18% tổng lượng call), nâng cao tối đa điểm số Efficiency trong bảng chấm.
5. **Bộ nhớ đệm theo phiên case (Per-Case Caching)**:
   - Lớp `RobustGateway` lưu bộ nhớ đệm mọi kết quả trả về từ gateway theo cặp `(tool_name, arguments)`. Trong cùng một case, không có tool nào bị gọi lặp lại lần thứ hai.
6. **Loại trừ thực thể giả không qua gateway (Candidate Pruning)**:
   - Phân tích `customer_history` để nhận diện và loại trừ ngay các ứng viên giả (`candidate-xxx`), không tốn lệnh gọi `get_order` để thăm dò.

## 5. Evidence và conflict lifecycle

- **Kiểm định MCP Response**: Mọi phản hồi đều được đối soát qua schema chuẩn `day09-mcp-evidence-v1`.
- **Liên kết Trace - Evidence**: Khi một tool thực thi thành công, sự kiện `tool_result_consumed` được phát sinh ngay lập tức trong `trace.jsonl` gắn liền với `evidence_ref` tương ứng.
- **Xử lý Source Conflict & Timeline Precedence**:
  - Khi ngày mua hàng trên snapshot `get_order` lớn hơn ngày mở hồ sơ khiếu nại (`order_purchase_timestamp > opened_at`), hệ thống xác định snapshot này phản ánh giao dịch sau khiếu nại.
  - Conflict Resolver ưu tiên chọn bản ghi lịch sử trước khiếu nại từ `get_customer_history` (`resolution_code: opened_at_precedence`).
  - Xung đột trạng thái đơn hàng giữa snapshot và lịch sử được phân giải bằng `authoritative_history_status`.

## 6. Verification invariants

Trước khi phê duyệt output, Agent 2 (`SpecialistAuditorAgent`) thực thi kiểm tra nghiêm ngặt 10 bất biến nghiệp vụ:
1. **Schema compliance**: Tuân thủ 100% JSON Schema `day09-l3b-output-v2`.
2. **Case ID consistency**: `output["case_id"] == case["case_id"]`.
3. **Entity scope**: `resolved_order_ids` là tập con của `candidate_order_ids`.
4. **Candidate partition**: `resolved_order_ids` và `rejected_candidates` tạo thành phân hoạch hoàn chỉnh của `candidate_order_ids`.
5. **Evidence ownership**: Mọi `evidence_ref` trong output phải thuộc phiên audit của chính case đó.
6. **Claim linkage**: Mỗi claim trong yêu cầu của khách hàng đều được đánh giá đầy đủ kèm bằng chứng trực tiếp.
7. **Timeline precedence**: Xung đột thời gian sau `opened_at` phải được ghi nhận chuẩn xác vào `data_conflicts`.
8. **Financial math integrity**: Tổng số tiền trong các dòng `refund_lines` phải khớp chính xác tuyệt đối với `recommended_refund_brl`.
9. **Status & Action coherence**: `no_action` tương ứng refund = 0 và hành động `document_no_action`; `action_required` có hành động cụ thể và định mức bồi thường phù hợp.
10. **Seller responsibility coherence**: Khi chủ đề là `late_delivery_seller`, `party_id` của bên chịu trách nhiệm phải trùng với seller trong `late_seller_ids`.

## 7. Reproducibility & Model Governance

- **Quy định kích thước mô hình**:
  - Agent 1: `Qwen/Qwen2.5-7B-Instruct` — **7.61B parameters** (< 10B).
  - Agent 2: `meta-llama/Llama-3.1-8B-Instruct` — **8.03B parameters** (< 10B).
  - Cả hai mô hình đều được ghi nhận tường minh trong trace event attributes (`attributes={"agent_model": ..., "params": ...}`).
- **Môi trường & Thư viện**:
  - Python 3.11+, ảo hóa `.venv`.
  - Dependencies: `httpx2`, `mcp`, `jsonschema`, `referencing`.
- **Tính tiền định (Determinism)**:
  - Logic phân loại, định tuyến và kiểm định hoàn toàn tiền định (zero unseeded stochasticity).
- **Lệnh thực thi**:
  ```bash
  day09 run
  day09 validate
  day09 package --output dist/submission.zip
  ```
- **Thời gian thực thi**: Toàn bộ 100 cases hoàn thành trong khoảng ~1.5 phút; tổng lưu lượng bộ nhớ < 200MB.
