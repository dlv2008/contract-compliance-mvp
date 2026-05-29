# 字段清单 MVP 版

## 1. 基础信息

- `contract_type`
- `contract_name`
- `signing_date`
- `party_a_name`
- `party_b_name`
- `party_b_uscc`

## 2. 金额与税票

- `amount_total`
- `currency`
- `tax_included`
- `invoice.type`
- `invoice.tax_rate`
- `invoice.issue_timing`

## 3. 付款

- `payment.prepay_ratio`
- `payment.prepay_due_days`
- `payment.final_ratio`
- `payment.final_due_days`
- `payment.final_condition`

## 4. 验收与履行

- `acceptance.required`
- `acceptance.deadline_days`
- `delivery.deadline`
- `warranty.present`
- `warranty.period_months`

## 5. 期限与解除

- `term.start_date`
- `term.end_date`
- `term.auto_renewal`
- `termination.unilateral_right`

## 6. 争议与责任

- `dispute.method`
- `dispute.location`
- `liability.cap_amount`
- `liability.reciprocal`

## 7. 账户与主体

- `account.payee_name`
- `account.bank_name`
- `account.bank_account`
- `account.same_as_counterparty`

## 8. 其他

- `confidentiality.present`
- `data_processing.present`
- `approval.exception_required`
- `supplement.priority_clause`
