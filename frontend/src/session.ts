import { api } from './api';
import type { Trip, TripRequest, Workspace } from './api';
import { Autosave } from './autosave';

export class TripSession {
  trip: Trip;
  request: Autosave<{ name: string; request: TripRequest }>;
  workspace: Autosave<Workspace>;
  composer = '';
  messageOperation: { id: string; content: string } | null = null;
  constructor(trip: Trip, onSaved: (trip: Trip) => void) {
    this.trip = trip;
    this.request = new Autosave({ name: trip.name, request: trip.request }, trip.request_version, async (draft, version) => {
      const saved = await api.update(trip.id, version, draft);
      this.trip = { ...this.trip, name: saved.name, request: saved.request, request_version: saved.request_version, updated_at: saved.updated_at };
      onSaved(this.trip);
      return { value: { name: saved.name, request: saved.request }, version: saved.request_version };
    });
    this.workspace = new Autosave(trip.workspace, trip.workspace_version, async (workspace, version) => {
      const saved = await api.workspace(trip.id, version, workspace);
      this.trip = { ...this.trip, workspace: saved.workspace, workspace_version: saved.workspace_version, updated_at: saved.updated_at };
      onSaved(this.trip);
      return { value: saved.workspace, version: saved.workspace_version };
    });
  }
  get dirty() { return this.request.dirty || this.workspace.dirty; }
  async flush() { await Promise.all([this.request.flush(), this.workspace.flush()]); }
  async retry() { await Promise.all([this.request.retry(), this.workspace.retry()]); }
  accept(trip: Trip) {
    this.trip = trip;
    this.request.resolve({ name: trip.name, request: trip.request }, trip.request_version, false);
    this.workspace.resolve(trip.workspace, trip.workspace_version, false);
  }
  dispose() { this.request.dispose(); this.workspace.dispose(); }
}
