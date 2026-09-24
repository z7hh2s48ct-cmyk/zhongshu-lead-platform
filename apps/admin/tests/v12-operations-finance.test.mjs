import assert from 'node:assert/strict';
import fs from 'node:fs';
import test from 'node:test';

const source = fs.readFileSync(new URL('../public/v12-operations.js', import.meta.url), 'utf8');
const functionSource = source.match(/function financeWithdrawalSection\([\s\S]*?\n}\n\nconst WITHDRAWAL_STATUS_LABEL/)[0]
  .replace(/\n\nconst WITHDRAWAL_STATUS_LABEL[\s\S]*$/, '');
const tableSource = source.match(/function table\([\s\S]*?\nconst rowSequence/)[0]
  .replace(/\nconst rowSequence[\s\S]*$/, '');

function loadFinanceWithdrawalSection() {
  const table = Function(`return (${tableSource})`)();
  const pager = () => '';
  const esc = value => String(value ?? '');
  const badge = () => '';
  const withdrawalStatusLabel = status => status;
  const fmt = value => value || '--';
  const module = `(function(table,pager,esc,badge,withdrawalStatusLabel,fmt){ return ${functionSource}; })`;
  return Function(`return ${module}`)()(table, pager, esc, badge, withdrawalStatusLabel, fmt);
}

test('资金页空提现列表仍能渲染', () => {
  const render = loadFinanceWithdrawalSection();
  const html = render({ items: [] }, null);

  assert.match(html, /暂无提现申请/);
});

test('资金页有一条提现记录时仍能渲染', () => {
  const render = loadFinanceWithdrawalSection();
  const html = render({ items: [{ id: 'withdrawal-1', company_id: 'company-1', status: 'PENDING_REVIEW', points_requested: 100 }] }, null);

  assert.match(html, /withdrawal-1/);
});

test('资金页有多条提现记录时仍能渲染', () => {
  const render = loadFinanceWithdrawalSection();
  const html = render({ items: [
    { id: 'withdrawal-1', company_id: 'company-1', status: 'PENDING_REVIEW', points_requested: 100 },
    { id: 'withdrawal-2', company_id: 'company-2', status: 'PAID', points_requested: 200 },
  ] }, null);

  assert.match(html, /withdrawal-1/);
  assert.match(html, /company-2/);
});
