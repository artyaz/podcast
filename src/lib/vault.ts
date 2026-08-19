import { browser } from '$app/environment';
import { backendUrl } from './settings.svelte';

/**
 * Client-side vault: a one-time key encrypts every row, and only this browser
 * ever sees plaintext. IndexedDB holds ciphertext. If the key is set, the same
 * rows are also pushed to `/api/vault/:id` and pulled on unlock so a second
 * device can catch up. The server stores opaque blobs addressed by a hash of
 * the key — not the key, and not anything it could decrypt.
 */

const READY_FLAG = 'praxis.vaultReady';
const SESSION_KEY = 'praxis.vaultKey';
const DB_NAME = 'praxis';
const STORE = 'rows';
const PBKDF2_SALT = 'praxis.vault.v1';
const PBKDF2_ITERATIONS = 120000;

export interface VaultRow {
	id: string;
	ciphertext: string;
	updated_at: number;
}

export const vault = $state({
	unlocked: false,
	hasVault: false,
	key: '',
	vaultId: '',
	busy: false,
	errorMessage: ''
});

let aesKey: CryptoKey | null = null;
const memoryPlain = new Map<string, unknown>();

function encoder() {
	return new TextEncoder();
}

function decoder() {
	return new TextDecoder();
}

function bytesToBase64(bytes: Uint8Array): string {
	let binary = '';
	for (const byte of bytes) binary += String.fromCharCode(byte);
	return btoa(binary);
}

function base64ToBytes(value: string): Uint8Array {
	const binary = atob(value);
	const bytes = new Uint8Array(binary.length);
	for (let index = 0; index < binary.length; index += 1) {
		bytes[index] = binary.charCodeAt(index);
	}
	return bytes;
}

function toHex(bytes: Uint8Array): string {
	return [...bytes].map((byte) => byte.toString(16).padStart(2, '0')).join('');
}

export function generateVaultKey(): string {
	const bytes = crypto.getRandomValues(new Uint8Array(16));
	const alphabet = 'abcdefghijkmnpqrstuvwxyz23456789';
	let raw = '';
	for (const byte of bytes) raw += alphabet[byte % alphabet.length];
	const groups = raw.match(/.{1,4}/g) || [raw];
	return `px_${groups.join('_')}`;
}

async function deriveAesKey(secret: string): Promise<CryptoKey> {
	const material = await crypto.subtle.importKey(
		'raw',
		encoder().encode(secret),
		'PBKDF2',
		false,
		['deriveKey']
	);
	return crypto.subtle.deriveKey(
		{
			name: 'PBKDF2',
			salt: encoder().encode(PBKDF2_SALT),
			iterations: PBKDF2_ITERATIONS,
			hash: 'SHA-256'
		},
		material,
		{ name: 'AES-GCM', length: 256 },
		false,
		['encrypt', 'decrypt']
	);
}

async function vaultIdOf(secret: string): Promise<string> {
	const digest = await crypto.subtle.digest(
		'SHA-256',
		encoder().encode(`praxis.vault:${secret}`)
	);
	return toHex(new Uint8Array(digest));
}

async function encryptPayload(plaintext: unknown, key: CryptoKey): Promise<string> {
	const iv = crypto.getRandomValues(new Uint8Array(12));
	const encoded = encoder().encode(JSON.stringify(plaintext));
	const cipher = await crypto.subtle.encrypt(
		{ name: 'AES-GCM', iv: iv as BufferSource },
		key,
		encoded as BufferSource
	);
	return `v1.${bytesToBase64(iv)}.${bytesToBase64(new Uint8Array(cipher))}`;
}

async function decryptPayload(envelope: string, key: CryptoKey): Promise<unknown> {
	const parts = envelope.split('.');
	if (parts.length !== 3 || parts[0] !== 'v1') {
		throw new Error('unrecognised ciphertext');
	}
	const iv = base64ToBytes(parts[1]);
	const data = base64ToBytes(parts[2]);
	const plain = await crypto.subtle.decrypt(
		{ name: 'AES-GCM', iv: iv as BufferSource },
		key,
		data as BufferSource
	);
	return JSON.parse(decoder().decode(plain));
}

function openDb(): Promise<IDBDatabase> {
	return new Promise((resolve, reject) => {
		const request = indexedDB.open(DB_NAME, 1);
		request.onupgradeneeded = () => {
			const db = request.result;
			if (!db.objectStoreNames.contains(STORE)) {
				db.createObjectStore(STORE, { keyPath: 'id' });
			}
		};
		request.onsuccess = () => resolve(request.result);
		request.onerror = () => reject(request.error);
	});
}

async function idbGetAll(): Promise<VaultRow[]> {
	const db = await openDb();
	return new Promise((resolve, reject) => {
		const request = db.transaction(STORE, 'readonly').objectStore(STORE).getAll();
		request.onsuccess = () => resolve((request.result as VaultRow[]) || []);
		request.onerror = () => reject(request.error);
	});
}

async function idbPut(row: VaultRow): Promise<void> {
	const db = await openDb();
	return new Promise((resolve, reject) => {
		const request = db.transaction(STORE, 'readwrite').objectStore(STORE).put(row);
		request.onsuccess = () => resolve();
		request.onerror = () => reject(request.error);
	});
}

function mergeRows(local: VaultRow[], remote: VaultRow[]): VaultRow[] {
	const byId = new Map<string, VaultRow>();
	for (const row of local) byId.set(row.id, row);
	for (const row of remote) {
		const held = byId.get(row.id);
		if (!held || row.updated_at >= held.updated_at) byId.set(row.id, row);
	}
	return [...byId.values()];
}

async function pullRemote(vaultId: string): Promise<VaultRow[]> {
	try {
		const response = await fetch(backendUrl(`/api/vault/${vaultId}`));
		if (!response.ok) return [];
		const payload = await response.json();
		return (payload.rows as VaultRow[]) || [];
	} catch {
		return [];
	}
}

async function pushRemote(vaultId: string, rows: VaultRow[]): Promise<void> {
	if (!rows.length) return;
	try {
		await fetch(backendUrl(`/api/vault/${vaultId}`), {
			method: 'PUT',
			headers: { 'content-type': 'application/json' },
			body: JSON.stringify({ rows })
		});
	} catch {
		// Local ciphertext is already saved; a later unlock will retry.
	}
}

export function vaultIsConfigured(): boolean {
	if (!browser) return false;
	return localStorage.getItem(READY_FLAG) === '1';
}

export function sessionVaultKey(): string {
	if (!browser) return '';
	return sessionStorage.getItem(SESSION_KEY) || '';
}

export async function unlockVault(secret: string): Promise<Record<string, unknown>> {
	const trimmed = secret.trim();
	if (!trimmed) throw new Error('Enter the vault key.');
	vault.busy = true;
	vault.errorMessage = '';
	try {
		const derived = await deriveAesKey(trimmed);
		const vaultId = await vaultIdOf(trimmed);
		const localRows = await idbGetAll();
		const remoteRows = await pullRemote(vaultId);
		const merged = mergeRows(localRows, remoteRows);

		const decrypted: Record<string, unknown> = {};
		for (const row of merged) {
			if (!row.ciphertext) continue;
			try {
				decrypted[row.id] = await decryptPayload(row.ciphertext, derived);
			} catch {
				if (localRows.some((candidate) => candidate.id === row.id)) {
					throw new Error('That key does not match this vault.');
				}
			}
			await idbPut(row);
		}

		const newerLocal = localRows.filter((row) => {
			const remote = remoteRows.find((candidate) => candidate.id === row.id);
			return !remote || row.updated_at > remote.updated_at;
		});
		await pushRemote(vaultId, newerLocal);

		aesKey = derived;
		memoryPlain.clear();
		for (const [rowId, value] of Object.entries(decrypted)) {
			memoryPlain.set(rowId, value);
		}
		vault.unlocked = true;
		vault.hasVault = true;
		vault.key = trimmed;
		vault.vaultId = vaultId;
		if (browser) {
			sessionStorage.setItem(SESSION_KEY, trimmed);
			localStorage.setItem(READY_FLAG, '1');
		}
		return decrypted;
	} catch (failure) {
		vault.errorMessage = (failure as Error).message;
		throw failure;
	} finally {
		vault.busy = false;
	}
}

export async function putPlain(rowId: string, value: unknown): Promise<void> {
	if (!aesKey || !vault.vaultId) return;
	memoryPlain.set(rowId, value);
	const ciphertext = await encryptPayload(value, aesKey);
	const row: VaultRow = { id: rowId, ciphertext, updated_at: Date.now() };
	await idbPut(row);
	void pushRemote(vault.vaultId, [row]);
}

export function readPlain<T>(rowId: string): T | undefined {
	return memoryPlain.get(rowId) as T | undefined;
}

export function lockVault(): void {
	aesKey = null;
	memoryPlain.clear();
	vault.unlocked = false;
	vault.key = '';
	vault.vaultId = '';
	if (browser) sessionStorage.removeItem(SESSION_KEY);
}

export function markVaultReady(): void {
	if (browser) localStorage.setItem(READY_FLAG, '1');
	vault.hasVault = true;
}
