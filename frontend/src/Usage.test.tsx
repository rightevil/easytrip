import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { api } from './api';
import { Usage } from './Usage';
import type { UsageReport } from './discovery-types';

const report = (calls: number): UsageReport => ({
  external_calls: calls, model_tokens: 1200, images: 2, unknown_cost_calls: 1, cost_cny: null,
  cache_hits: 3, failed_calls: 1, unknown_result_calls: 2, scope: 'recorded_application_calls', account_total_known: false, pricing_status: 'unverified',
  providers: { rail: { external_calls: calls, model_tokens: 0, images: 0, unknown_cost_calls: 0, cost_cny: null } },
  runs: [{ id: 'run-one', first_candidates_at: null, interrupted_stage_time_unknown: true }], actions: [{ id: 'in-flight', status: 'started' }],
});
afterEach(() => { cleanup(); vi.restoreAllMocks(); });

describe('Usage', () => {
  it('reads current trip usage on request without representing token as money', async () => {
    const query = vi.spyOn(api, 'usage').mockResolvedValue(report(7));
    render(<Usage tripId="one" />);
    expect(query).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole('button', { name: '读取最新用量' }));
    await screen.findByText(/已记录外部调用 7/);
    expect(query).toHaveBeenCalledWith('one');
    expect(screen.getByText(/人民币费用未核定/)).toBeDefined();
    expect(screen.getByText(/结果未知 2/).textContent).toContain('在途 1 次');
    expect(screen.getByText(/铁路：7 次业务调用/)).toBeDefined();
    expect(screen.getByRole('link', { name: '查看脱敏用量报告' }).getAttribute('href')).toBe('/api/trips/one/usage');
  });
  it('keeps unavailable usage unknown', async () => {
    vi.spyOn(api, 'usage').mockRejectedValue(new Error('服务暂不可用'));
    render(<Usage tripId="one" />);
    fireEvent.click(screen.getByRole('button'));
    expect((await screen.findByRole('alert')).textContent).toContain('当前用量未知');
    expect(screen.queryByText(/已记录外部调用/)).toBeNull();
  });
  it('discards a previous trip response', async () => {
    let resolveOld!: (value: UsageReport) => void;
    const query = vi.spyOn(api, 'usage').mockImplementationOnce(() => new Promise(resolve => { resolveOld = resolve; })).mockResolvedValue(report(2));
    const view = render(<Usage tripId="one" />);
    fireEvent.click(screen.getByRole('button'));
    view.rerender(<Usage tripId="two" />);
    fireEvent.click(screen.getByRole('button'));
    await screen.findByText(/已记录外部调用 2/);
    await act(async () => { resolveOld(report(99)); });
    await waitFor(() => { expect(query).toHaveBeenCalledWith('two'); });
    expect(screen.queryByText(/已记录外部调用 99/)).toBeNull();
  });
});
