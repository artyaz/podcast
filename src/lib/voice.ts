import { absorbUsage, backendUrl, llmPayload, secretsPayload } from './settings.svelte';

/**
 * Browser recording + OpenRouter transcription, shared by the ask bar and the
 * in-block ask. Safari records mp4; Chrome and Firefox prefer webm/opus.
 */

let activeRecorder: MediaRecorder | null = null;
let activeStream: MediaStream | null = null;
let recordedChunks: Blob[] = [];
let recordedMime = 'audio/webm';

export function pickRecordingMimeType(): string {
	const candidates = ['audio/webm;codecs=opus', 'audio/webm', 'audio/mp4'];
	for (const candidate of candidates) {
		if (MediaRecorder.isTypeSupported(candidate)) return candidate;
	}
	return '';
}

export async function startRecording(): Promise<void> {
	if (activeRecorder) stopRecording();
	const stream = await navigator.mediaDevices.getUserMedia({ audio: true });
	const mimeType = pickRecordingMimeType();
	recordedMime = mimeType || 'audio/webm';
	recordedChunks = [];
	activeStream = stream;
	const recorder = new MediaRecorder(stream, mimeType ? { mimeType } : undefined);
	recorder.ondataavailable = (event) => {
		if (event.data.size) recordedChunks.push(event.data);
	};
	recorder.start();
	activeRecorder = recorder;
}

export function stopRecording(): Promise<Blob> {
	const recorder = activeRecorder;
	const stream = activeStream;
	activeRecorder = null;
	activeStream = null;
	if (!recorder) return Promise.resolve(new Blob());
	return new Promise((resolve) => {
		recorder.onstop = () => {
			stream?.getTracks().forEach((track) => track.stop());
			resolve(new Blob(recordedChunks, { type: recordedMime }));
		};
		if (recorder.state === 'inactive') {
			stream?.getTracks().forEach((track) => track.stop());
			resolve(new Blob(recordedChunks, { type: recordedMime }));
			return;
		}
		recorder.stop();
	});
}

export async function transcribeBlob(audioBlob: Blob): Promise<string> {
	if (!audioBlob.size) throw new Error('Empty recording.');
	const extension = audioBlob.type.includes('mp4')
		? 'mp4'
		: audioBlob.type.includes('wav')
			? 'wav'
			: 'webm';
	const formData = new FormData();
	formData.append('audio', audioBlob, `question.${extension}`);
	formData.append('secrets', JSON.stringify(secretsPayload()));
	formData.append('llm', JSON.stringify(llmPayload()));

	const response = await fetch(backendUrl('/api/transcribe'), {
		method: 'POST',
		body: formData
	});
	if (!response.ok) {
		let detail = `HTTP ${response.status}`;
		try {
			detail = (await response.json()).detail || detail;
		} catch {
			/* keep the status line */
		}
		throw new Error(detail);
	}
	const payload = await response.json();
	absorbUsage(payload.usage);
	const text = (payload.text || '').trim();
	if (!text) throw new Error('Nothing was audible in that recording.');
	return text;
}
