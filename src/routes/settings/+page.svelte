<script lang="ts">
	import {
		PROVIDER_NAMES,
		absorbUsage,
		backendUrl,
		exportConfig,
		fingerprintOf,
		importConfig,
		keysAsText,
		save,
		secretsPayload,
		setKeysFromText,
		settings,
		type ProviderName
	} from '$lib/settings.svelte';

	interface VoiceOption {
		id: string;
		label: string;
		accent?: string;
		gender?: string;
		locale?: string;
		models?: string[];
	}

	let kokoroVoices = $state<VoiceOption[]>([]);
	let speechifyVoices = $state<VoiceOption[]>([]);
	let voicesMessage = $state('');
	let loadingVoices = $state(false);

	let testMessage = $state('');
	let testing = $state(false);

	let exported = $state('');
	let importDraft = $state('');
	let importMessage = $state('');

	const providerLabels: Record<ProviderName, string> = {
		openrouter: 'OpenRouter — model, transcription, and Kokoro speech',
		exa: 'Exa — search',
		firecrawl: 'Firecrawl — full page reads',
		speechify: 'Speechify — higher-quality speech (optional)'
	};

	const providerHints: Record<ProviderName, string> = {
		openrouter: 'One key per line. Required.',
		exa: 'One key per line. Required. No balance endpoint exists, so spend is tracked here per key.',
		firecrawl:
			'One key per line. Optional but strongly recommended — without it the agent can only read search snippets, never a full statute or paper.',
		speechify: 'One key per line. Optional. Gives word-level timings that Kokoro cannot.'
	};

	/** Locales worth surfacing first; the rest stay available in the full list. */
	const speechifyLocaleGroups = $derived.by(() => {
		const groups = new Map<string, VoiceOption[]>();
		for (const voice of speechifyVoices) {
			const locale = voice.locale || 'other';
			if (!groups.has(locale)) groups.set(locale, []);
			groups.get(locale)!.push(voice);
		}
		return [...groups.entries()].sort((left, right) => {
			const rank = (locale: string) => (locale.startsWith('en') ? 0 : 1);
			return rank(left[0]) - rank(right[0]) || left[0].localeCompare(right[0]);
		});
	});

	async function loadVoices() {
		loadingVoices = true;
		voicesMessage = '';
		try {
			const response = await fetch(backendUrl('/api/voices'), {
				method: 'POST',
				headers: { 'content-type': 'application/json' },
				body: JSON.stringify({ secrets: secretsPayload() })
			});
			if (!response.ok) throw new Error(`HTTP ${response.status}`);
			const payload = await response.json();
			kokoroVoices = payload.kokoro || [];
			speechifyVoices = payload.speechify || [];
			absorbUsage(payload.usage);
			voicesMessage = payload.speechify_error
				? `Kokoro voices loaded. Speechify said: ${payload.speechify_error}`
				: `${kokoroVoices.length} Kokoro voices, ${speechifyVoices.length} Speechify voices.`;
		} catch (failure) {
			voicesMessage = `Could not reach the backend: ${(failure as Error).message}`;
		} finally {
			loadingVoices = false;
		}
	}

	async function testBackend() {
		testing = true;
		testMessage = '';
		try {
			const response = await fetch(backendUrl('/api/health'));
			if (!response.ok) throw new Error(`HTTP ${response.status}`);
			const payload = await response.json();
			testMessage = `✓ Backend alive. Default model: ${payload.default_model}`;
		} catch (failure) {
			testMessage = `✗ ${(failure as Error).message}`;
		} finally {
			testing = false;
		}
	}

	function copyConfig() {
		exported = exportConfig();
		navigator.clipboard?.writeText(exported).catch(() => {
			/* the textarea below is the fallback */
		});
	}

	function doImport() {
		importMessage = importConfig(importDraft)
			? '✓ Imported.'
			: '✗ That code did not parse.';
		if (importMessage.startsWith('✓')) importDraft = '';
	}
</script>

<svelte:head><title>Settings · Praxis</title></svelte:head>

<div class="page">
	<h1>Settings</h1>

	<p class="warn">
		Every key here is stored in <strong>this browser only</strong> and sent with each request.
		Nothing is baked into the deployment and nothing is stored on the server. Add several keys per
		provider and they are rotated automatically — Firecrawl by its real remaining credits,
		the others by least-spent-first, and any key is dropped for the rest of a request as soon as it
		answers with rejected, out-of-credit, or rate-limited.
	</p>

	{#each PROVIDER_NAMES as provider (provider)}
		<section>
			<h2>{providerLabels[provider]}</h2>
			<textarea
				rows="3"
				value={keysAsText(provider)}
				oninput={(event) => setKeysFromText(provider, event.currentTarget.value)}
				placeholder="one key per line"
				autocapitalize="none"
				autocorrect="off"
				autocomplete="off"
				spellcheck="false"
			></textarea>
			<p class="hint">{providerHints[provider]}</p>

			{#if (settings.keys[provider] || []).length}
				<div class="usage">
					{#each settings.keys[provider] as apiKey (apiKey)}
						{@const record = settings.usage[provider]?.[fingerprintOf(apiKey)]}
						<div class="usagerow">
							<code>{fingerprintOf(apiKey)}</code>
							<span>
								{#if record}
									{record.calls ?? 0} calls
									{#if (record.spent_dollars ?? 0) > 0}
										· ${(record.spent_dollars ?? 0).toFixed(4)}
									{/if}
									{#if record.remaining_credits != null}
										· {record.remaining_credits} credits left
									{/if}
									{#if (record.characters ?? 0) > 0}
										· {record.characters} chars spoken
									{/if}
								{:else}
									unused
								{/if}
							</span>
						</div>
					{/each}
				</div>
			{/if}
		</section>
	{/each}

	<section>
		<h2>Model</h2>
		<label>
			<span>Chat model on OpenRouter</span>
			<input
				bind:value={settings.model}
				onchange={save}
				autocapitalize="none"
				autocorrect="off"
				spellcheck="false"
			/>
		</label>
		<p class="hint">
			The default is the non-reasoning DeepSeek v4 Flash alias, which resolves to a dated build and
			runs with reasoning explicitly switched off.
		</p>
	</section>

	<section>
		<h2>Voice</h2>
		<div class="row">
			<button
				class="ghost"
				class:on={settings.speechProvider === 'kokoro'}
				onclick={() => {
					settings.speechProvider = 'kokoro';
					save();
				}}
			>
				Kokoro
			</button>
			<button
				class="ghost"
				class:on={settings.speechProvider === 'speechify'}
				onclick={() => {
					settings.speechProvider = 'speechify';
					save();
				}}
			>
				Speechify
			</button>
			<button class="ghost" onclick={loadVoices} disabled={loadingVoices}>
				{loadingVoices ? 'Loading…' : 'Load voices'}
			</button>
		</div>
		{#if voicesMessage}<p class="status">{voicesMessage}</p>{/if}

		{#if settings.speechProvider === 'kokoro'}
			<label>
				<span>Kokoro voice and accent</span>
				<select bind:value={settings.kokoroVoice} onchange={save}>
					{#if kokoroVoices.length}
						{#each kokoroVoices as voice (voice.id)}
							<option value={voice.id}>
								{voice.label} — {voice.accent} {voice.gender}
							</option>
						{/each}
					{:else}
						<option value="af_heart">Heart — American female</option>
						<option value="af_bella">Bella — American female</option>
						<option value="af_nicole">Nicole — American female</option>
						<option value="am_michael">Michael — American male</option>
						<option value="am_adam">Adam — American male</option>
						<option value="bf_emma">Emma — British female</option>
						<option value="bm_george">George — British male</option>
					{/if}
				</select>
			</label>
			<p class="hint">
				Kokoro publishes no voice list, so these seven were confirmed one by one against the live
				endpoint. The prefix is the accent: af and am are American, bf and bm are British.
			</p>
		{:else}
			<label>
				<span>Speechify voice</span>
				<select bind:value={settings.speechifyVoice} onchange={save}>
					{#if speechifyVoices.length}
						{#each speechifyLocaleGroups as [locale, voices] (locale)}
							<optgroup label={locale}>
								{#each voices as voice (voice.id)}
									<option value={voice.id}>{voice.label} — {voice.gender}</option>
								{/each}
							</optgroup>
						{/each}
					{:else}
						<option value="alec">Alec — en-GB male</option>
						<option value="alicia">Alicia — en-US female</option>
					{/if}
				</select>
			</label>
			<label>
				<span>Speechify model</span>
				<select bind:value={settings.speechifyModel} onchange={save}>
					<option value="simba-3.0">simba-3.0</option>
					<option value="simba-english">simba-english</option>
					<option value="simba-multilingual">simba-multilingual</option>
				</select>
			</label>
			<p class="hint">Press “Load voices” to pull the live list, including non-English locales.</p>
		{/if}
	</section>

	<section>
		<h2>How hard it researches</h2>
		<label>
			<span>Minimum research rounds — the model cannot skip these, however confident it is</span>
			<input type="number" min="1" max="6" bind:value={settings.minimumRounds} onchange={save} />
		</label>
		<label>
			<span>Maximum research rounds</span>
			<input type="number" min="1" max="10" bind:value={settings.maximumRounds} onchange={save} />
		</label>
		<label>
			<span>Blocks to aim for in an episode</span>
			<input type="number" min="4" max="40" bind:value={settings.targetBlockCount} onchange={save} />
		</label>
		<label>
			<span>Seconds per pass</span>
			<input type="number" min="30" max="280" bind:value={settings.budgetSeconds} onchange={save} />
		</label>
		<p class="hint">
			Vercel's Hobby plan kills any request at 300 seconds, so research runs in passes: the graph
			stops just short of the limit, hands back its state, and the next pass picks up where it left
			off. Leave this at 240 unless you are on Pro, where 800 is available.
		</p>
	</section>

	<section>
		<h2>Backend</h2>
		<label>
			<span>Backend URL — leave empty if the Python service is on this same domain</span>
			<input
				bind:value={settings.backendBaseUrl}
				onchange={save}
				placeholder="https://your-praxis-api.vercel.app"
				autocapitalize="none"
				autocorrect="off"
				spellcheck="false"
			/>
		</label>
		<div class="row">
			<button class="ghost" onclick={testBackend} disabled={testing}>
				{testing ? 'Testing…' : 'Test backend'}
			</button>
		</div>
		{#if testMessage}<p class="status" class:ok={testMessage.startsWith('✓')}>{testMessage}</p>{/if}
	</section>

	<section>
		<h2>Move to another device</h2>
		<p class="hint">This code contains your keys. Only share it with yourself.</p>
		<div class="row">
			<button class="ghost" onclick={copyConfig}>Copy my config</button>
		</div>
		{#if exported}<textarea rows="2" readonly value={exported}></textarea>{/if}
		<label>
			<span>Paste a config code</span>
			<textarea bind:value={importDraft} rows="2" placeholder="praxis:…" spellcheck="false"
			></textarea>
		</label>
		<div class="row">
			<button class="ghost" onclick={doImport} disabled={!importDraft.trim()}>Import</button>
		</div>
		{#if importMessage}<p class="status" class:ok={importMessage.startsWith('✓')}>
				{importMessage}
			</p>{/if}
	</section>
</div>

<style>
	.page {
		display: flex;
		flex-direction: column;
		gap: 28px;
		padding: 4px 0 60px;
	}
	h1 {
		font-family: var(--serif);
		font-size: 29px;
		margin: 4px 0 0;
	}
	.warn {
		margin: 0;
		font-size: 13px;
		line-height: 1.55;
		color: var(--muted);
		border: 1px solid var(--line);
		border-radius: 12px;
		padding: 12px 14px;
	}
	.warn strong {
		color: var(--ink);
	}
	section {
		display: flex;
		flex-direction: column;
		gap: 11px;
	}
	h2 {
		font-size: 12px;
		text-transform: uppercase;
		letter-spacing: 0.08em;
		color: var(--muted);
		margin: 0;
		line-height: 1.4;
	}
	.hint {
		font-size: 13px;
		color: var(--muted);
		margin: 0;
		line-height: 1.5;
	}
	label {
		display: flex;
		flex-direction: column;
		gap: 6px;
	}
	label span {
		font-size: 13px;
		color: var(--muted);
		line-height: 1.45;
	}
	input,
	textarea,
	select {
		padding: 12px 15px;
		border: 1.5px solid var(--line);
		border-radius: 13px;
		background: var(--panel);
		color: var(--ink);
		outline: none;
		width: 100%;
		font-family: inherit;
		line-height: 1.5;
		resize: vertical;
	}
	input:focus,
	textarea:focus,
	select:focus {
		border-color: color-mix(in srgb, var(--ink) 45%, var(--line));
	}
	.row {
		display: flex;
		gap: 9px;
		flex-wrap: wrap;
	}
	.ghost {
		padding: 11px 17px;
		border-radius: 13px;
		border: 1.5px solid var(--line);
		font-size: 14px;
	}
	.ghost.on {
		border-color: var(--ink);
		background: color-mix(in srgb, var(--ink) 8%, transparent);
	}
	.ghost:disabled {
		opacity: 0.45;
	}
	.status {
		font-size: 13px;
		color: var(--muted);
		margin: 0;
		line-height: 1.5;
		word-break: break-word;
	}
	.status.ok {
		color: var(--ok);
	}
	.usage {
		display: flex;
		flex-direction: column;
		gap: 5px;
	}
	.usagerow {
		display: flex;
		justify-content: space-between;
		gap: 10px;
		font-size: 12px;
		color: var(--muted);
		border-top: 1px solid var(--line);
		padding-top: 5px;
	}
	.usagerow code {
		font-size: 11.5px;
	}
</style>
