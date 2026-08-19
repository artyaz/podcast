<script lang="ts">
	import { browser } from '$app/environment';
	import { hydrateLibrary } from '$lib/library.svelte';
	import { hydrateSettings, onSettingsChange, save, settings } from '$lib/settings.svelte';
	import {
		generateVaultKey,
		markVaultReady,
		putPlain,
		sessionVaultKey,
		unlockVault,
		vault,
		vaultIsConfigured
	} from '$lib/vault';

	let { children } = $props();

	let mode = $state<'loading' | 'create' | 'unlock' | 'open'>('loading');
	let generated = $state('');
	let entered = $state('');
	let copied = $state(false);
	let confirmed = $state(false);

	function wirePersist() {
		onSettingsChange(() => {
			void putPlain('settings', JSON.parse(JSON.stringify(settings)));
		});
		save();
	}

	async function applyRows(rows: Record<string, unknown>) {
		if (rows.settings) hydrateSettings(rows.settings as typeof settings);
		hydrateLibrary(rows);
		wirePersist();
	}

	async function boot() {
		if (!browser) return;
		vault.hasVault = vaultIsConfigured();
		const existing = sessionVaultKey();
		if (existing) {
			try {
				const rows = await unlockVault(existing);
				await applyRows(rows);
				mode = 'open';
				return;
			} catch {
				/* fall through to the lock screen */
			}
		}
		if (vault.hasVault) {
			mode = 'unlock';
			return;
		}
		generated = generateVaultKey();
		mode = 'create';
	}

	async function confirmCreate() {
		if (!confirmed) return;
		try {
			markVaultReady();
			const rows = await unlockVault(generated);
			await applyRows(rows);
			// Migrate whatever this browser already had in plaintext.
			await putPlain('settings', JSON.parse(JSON.stringify(settings)));
			mode = 'open';
		} catch (failure) {
			vault.errorMessage = (failure as Error).message;
		}
	}

	async function submitUnlock() {
		try {
			const rows = await unlockVault(entered);
			await applyRows(rows);
			mode = 'open';
		} catch {
			/* unlockVault stored the message */
		}
	}

	async function copyKey() {
		try {
			await navigator.clipboard.writeText(generated);
			copied = true;
		} catch {
			copied = false;
		}
	}

	let started = false;
	$effect(() => {
		if (started) return;
		started = true;
		void boot();
	});
</script>

{#if mode === 'open'}
	{@render children()}
{:else}
	<div class="gate">
		<h1>Your vault key</h1>
		{#if mode === 'create'}
			<p>
				This key encrypts every lesson and every API key in this browser. The server stores only
				ciphertext. If you lose the key, the data is gone — there is no reset.
			</p>
			<code class="key">{generated}</code>
			<div class="row">
				<button class="ghost" onclick={copyKey}>{copied ? 'Copied' : 'Copy key'}</button>
			</div>
			<label class="check">
				<input type="checkbox" bind:checked={confirmed} />
				I have saved this key somewhere I will find it.
			</label>
			<button class="primary" disabled={!confirmed || vault.busy} onclick={confirmCreate}>
				Unlock
			</button>
		{:else if mode === 'unlock'}
			<p>Enter the key that was generated for this vault.</p>
			<input
				bind:value={entered}
				spellcheck="false"
				autocomplete="off"
				placeholder="px_••••_••••_••••_••••"
				onkeydown={(event) => event.key === 'Enter' && submitUnlock()}
			/>
			<button class="primary" disabled={!entered.trim() || vault.busy} onclick={submitUnlock}>
				Unlock
			</button>
		{:else}
			<p class="muted">Opening the vault…</p>
		{/if}
		{#if vault.errorMessage}
			<p class="error">{vault.errorMessage}</p>
		{/if}
	</div>
{/if}

<style>
	.gate {
		display: flex;
		flex-direction: column;
		gap: 14px;
		padding: 24px 0 40px;
	}
	h1 {
		font-family: var(--serif);
		font-size: 28px;
		margin: 0;
	}
	p {
		margin: 0;
		font-size: 15px;
		line-height: 1.6;
		color: var(--muted);
	}
	.key {
		display: block;
		padding: 14px 16px;
		border: 1.5px solid var(--line);
		border-radius: 13px;
		background: var(--panel);
		font-size: 15px;
		letter-spacing: 0.04em;
		word-break: break-all;
	}
	.row {
		display: flex;
		gap: 8px;
	}
	.ghost,
	.primary {
		padding: 12px 18px;
		border-radius: 13px;
		font-weight: 600;
		width: fit-content;
	}
	.ghost {
		border: 1.5px solid var(--line);
	}
	.primary {
		background: var(--ink);
		color: var(--bg);
	}
	.primary:disabled {
		opacity: 0.4;
	}
	.check {
		display: flex;
		gap: 10px;
		align-items: flex-start;
		font-size: 14px;
		line-height: 1.45;
	}
	input:not([type='checkbox']) {
		padding: 14px 16px;
		border: 1.5px solid var(--line);
		border-radius: 13px;
		background: var(--panel);
		color: var(--ink);
		outline: none;
	}
	.error {
		color: var(--bad);
	}
	.muted {
		color: var(--muted);
	}
</style>
