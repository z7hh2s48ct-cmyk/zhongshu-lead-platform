import assert from 'node:assert/strict';
import fs from 'node:fs';
import test from 'node:test';

// 问题一·改善提示：公海列表需区分「仍无接收方」与「已找到可接收方，待重新匹配」，
// 且不得把重新匹配显示成已派发。锁定 publicPoolRematchState / publicPoolValidationText 行为。
const source = fs.readFileSync(new URL('../public/v12-operations.js', import.meta.url), 'utf8');
const block = source
  .match(/function publicPoolRematchState\([\s\S]*?\nfunction publicPoolTelesalesBlockReason/)[0]
  .replace(/\nfunction publicPoolTelesalesBlockReason[\s\S]*$/, '');

function loadHelpers() {
  return Function(`${block}; return {publicPoolRematchState, publicPoolValidationText};`)();
}

const supplierInPool = errors => ({
  source_kind: 'SUPPLIER_H5',
  status: 'PUBLIC_POOL',
  pending_reason: 'PUBLIC_POOL_NO_LOCAL_RECEIVER',
  public_pool_validation_errors: errors,
});

test('已出现合格接收方时标记为待重新匹配', () => {
  const { publicPoolRematchState, publicPoolValidationText } = loadHelpers();
  const item = supplierInPool({});

  assert.equal(publicPoolRematchState(item), 'RECEIVER_AVAILABLE');
  assert.match(publicPoolValidationText(item), /已找到可接收加盟商，待重新匹配转入派发池/);
  // 不得把重新匹配显示成已派发。
  assert.doesNotMatch(publicPoolValidationText(item), /已派发|已进入派发池/);
});

test('仍无接收方时保留明确阻断原因', () => {
  const { publicPoolRematchState, publicPoolValidationText } = loadHelpers();
  const item = supplierInPool({ receiver_coverage: '当地暂无可接收加盟商' });

  assert.equal(publicPoolRematchState(item), 'NO_RECEIVER');
  assert.match(publicPoolValidationText(item), /当地暂无可接收加盟商/);
});

test('非加盟商提交或非公海状态不参与重匹配区分', () => {
  const { publicPoolRematchState } = loadHelpers();

  assert.equal(publicPoolRematchState({ source_kind: 'PLATFORM_MANUAL', status: 'DRAFT' }), '');
  assert.equal(
    publicPoolRematchState({ source_kind: 'SUPPLIER_H5', status: 'READY_DISPATCH', pending_reason: null }),
    '',
  );
});
