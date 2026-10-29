import { useEffect, useState } from 'react';
import { api, message } from './api';
import type { CaseDetail } from './types';
import { ExplanationView } from './CaseDrawer';
import { Icon, State } from './ui';
type PatientPayload = Pick<CaseDetail, 'source' | 'result'> & { case: Pick<CaseDetail, 'id' | 'packet_title' | 'status' | 'source_hash' | 'artifact_hash' | 'updated_at'> };
export default function Patient({ share }: { share: string }) {
  const [data, setData] = useState<PatientPayload | null>(null); const [error, setError] = useState(''); const [revision, setRevision] = useState(0);
  useEffect(() => { const controller = new AbortController(); setError(''); void api<PatientPayload>(`/patient/${encodeURIComponent(share)}`, undefined, controller.signal).then(setData).catch(reason => { if (!controller.signal.aborted) setError(message(reason)); }); return () => controller.abort(); }, [share, revision]);
  return <main className="patient-page"><header><a href="/console/" className="brand"><span className="brand-mark"><Icon name="shield"/></span><span>Atropos <strong>MedPlain</strong></span></a><span className="subtle-tag">Read-only patient view</span></header>{error ? <State kind="error" title="Patient artifact unavailable" retry={() => setRevision(value => value + 1)}>{error}</State> : !data ? <State kind="loading" title="Reading released explanation"/> : data.case.status !== 'released' || !data.result?.explanation ? <State kind="empty" title="No released explanation is available"/> : <article className="card patient-document"><div className="eyebrow">SYNTHETIC EXAMPLE ONLY</div><h1>{data.case.packet_title || data.source?.title || 'Your discharge explanation'}</h1><p className="notice notice-success"><Icon name="shield"/>The released explanation matched the supported source facts. This checks fidelity, not whether the original care plan is clinically appropriate.</p><ExplanationView explanation={data.result.explanation}/><p className="muted">No real patient data is accepted. Ask your clinician or pharmacist about your own medicines or symptoms.</p></article>}</main>;
}
