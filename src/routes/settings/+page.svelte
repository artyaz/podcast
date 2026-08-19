<script lang="ts">
	import {
		PROVIDER_NAMES,
		absorbUsage,
		availableEfforts,
		backendUrl,
		exportConfig,
		fingerprintOf,
		importConfig,
		keysAsText,
		llmPayload,
		modelHasReasoning,
		modelSupportsTokenBudget,
		reasoningIsMandatory,
		reasoningPayload,
		save,
		secretsPayload,
		selectLlmProvider,
		selectModel,
		setKeysFromText,
		settings,
		type ModelCapability,
		type ProviderName
	} from '$lib/settings.svelte';

	let modelCatalogue = $state<ModelCapability[]>([]);
	let modelQuery = $state('');
	let modelsMessage = $state('');
	let loadingModels = $state(false);

	/**
	 * Search across name and id so both "gemini flash" and "google/" find things,
	 * and every whitespace-separated word has to match somewhere — typing
	 * "deepseek flash" should narrow rather than widen.
	 */
	const visibleModels = $derived.by(() => {
		const words = modelQuery.toLowerCase().split(/\s+/).filter(Boolean);
		const matches = modelCatalogue.filter((candidate) => {
			if (!words.length) return true;
			const haystack = `${candidate.id} ${candidate.name ?? ''}`.toLowerCase();
			return words.every((word) => haystack.includes(word));
		});
		// The list is 400-plus entries; rendering all of them makes the page crawl.
		return matches.slice(0, 40);
	});

	async function loadModels() {
		loadingModels = true;
		modelsMessage = '';
		try {
			const response = await fetch(backendUrl('/api/models'), {
				method: 'POST',
				headers: { 'content-type': 'application/json' },
				body: JSON.stringify({ secrets: secretsPayload(), llm: llmPayload() })
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
			modelCatalogue = payload.models || [];
			absorbUsage(payload.usage);
			modelsMessage = `${modelCatalogue.length} models available.`;

			// Re-adopt the saved model so its capabilities are refreshed. Without this
			// a model chosen before this catalogue existed keeps a null descriptor and
			// the reasoning control has nothing to work from.
			const current = modelCatalogue.find((candidate) => candidate.id === settings.model);
			if (current) selectModel(current);
		} catch (failure) {
			modelsMessage = `Could not read the model list: ${(failure as Error).message}`;
		} finally {
			loadingModels = false;
		}
	}

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
		<h2>Endpoint</h2>
		<div class="row">
			<button
				class="ghost"
				class:on={settings.llmProvider === 'openrouter'}
				onclick={() => {
					selectLlmProvider('openrouter');
					modelCatalogue = [];
				}}
			>
				OpenRouter
			</button>
			<button
				class="ghost"
				class:on={settings.llmProvider === 'openai_compatible'}
				onclick={() => {
					selectLlmProvider('openai_compatible');
					modelCatalogue = [];
				}}
			>
				OpenAI-compatible
			</button>
		</div>

		{#if settings.llmProvider === 'openai_compatible'}
			<label>
				<span>Base URL — the part before /chat/completions</span>
				<input
					bind:value={settings.llmBaseUrl}
					onchange={save}
					placeholder="https://api.openai.com/v1"
					autocapitalize="none"
					autocorrect="off"
					spellcheck="false"
				/>
			</label>
			<p class="hint">
				Chat, transcription, speech, and the model list are all read from this base. A server that
				only implements chat still works — the model picker just falls back to a plain list, and
				transcription or speech will fail if that server does not serve those paths.
			</p>
		{/if}
	</section>

	<section>
		<h2>Model</h2>
		<div class="row">
			<button class="ghost" onclick={loadModels} disabled={loadingModels}>
				{loadingModels ? 'Loading…' : 'Load models'}
			</button>
			<span class="currentmodel">{settings.model || 'none selected'}</span>
		</div>
		{#if modelsMessage}<p class="status">{modelsMessage}</p>{/if}

		{#if modelCatalogue.length}
			<input
				bind:value={modelQuery}
				placeholder="Search {modelCatalogue.length} models — name, vendor, id"
				autocapitalize="none"
				autocorrect="off"
				spellcheck="false"
			/>
			<div class="results">
				{#each visibleModels as candidate (candidate.id)}
					<button
						class="modelrow"
						class:chosen={candidate.id === settings.model}
						onclick={() => {
							selectModel(candidate);
							modelQuery = '';
						}}
					>
						<span class="modelname">{candidate.name || candidate.id}</span>
						<span class="modelmeta">
							{candidate.id}
							{#if !candidate.supports_tools}· no tools{/if}
							{#if candidate.reasoning?.mandatory}· reasoning forced{/if}
							{#if candidate.supports_reasoning && !candidate.reasoning?.mandatory}· reasoning optional{/if}
							{#if !candidate.supports_reasoning}· no reasoning{/if}
						</span>
					</button>
				{/each}
				{#if !visibleModels.length}
					<p class="hint">Nothing matches “{modelQuery}”.</p>
				{/if}
			</div>
			<p class="hint">
				A model without tool support cannot research anything — the whole loop is tool calls — so
				those are marked and best avoided.
			</p>
		{/if}
	</section>

	<section>
		<h2>Reasoning</h2>
		{#if !settings.modelCapability}
			<p class="hint">
				Load the models and pick one. The reasoning options are built from what that specific model
				reports about itself, because the wrong shape is not ignored: turning reasoning off on a
				model that requires it returns an error, and an effort value the model never advertised gets
				silently reinterpreted.
			</p>
		{:else if !modelHasReasoning()}
			<p class="hint">
				This model has no reasoning mode, so there is nothing to configure. No reasoning parameter
				is sent.
			</p>
		{:else}
			<div class="row">
				{#if !reasoningIsMandatory()}
					<button
						class="ghost"
						class:on={settings.reasoningMode === 'off'}
						onclick={() => {
							settings.reasoningMode = 'off';
							save();
						}}
					>
						Off
					</button>
				{/if}
				{#if availableEfforts().length}
					<button
						class="ghost"
						class:on={settings.reasoningMode === 'effort'}
						onclick={() => {
							settings.reasoningMode = 'effort';
							save();
						}}
					>
						Effort
					</button>
				{/if}
				{#if modelSupportsTokenBudget()}
					<button
						class="ghost"
						class:on={settings.reasoningMode === 'tokens'}
						onclick={() => {
							settings.reasoningMode = 'tokens';
							save();
						}}
					>
						Token budget
					</button>
				{/if}
			</div>

			{#if reasoningIsMandatory()}
				<p class="hint">
					This model cannot have reasoning switched off — asking for that is rejected outright — so
					there is no Off option.
				</p>
			{/if}

			{#if settings.reasoningMode === 'effort' && availableEfforts().length}
				<label>
					<span>Effort — only the values this model actually accepts</span>
					<select bind:value={settings.reasoningEffort} onchange={save}>
						{#each availableEfforts() as effort (effort)}
							<option value={effort}>
								{effort}{settings.modelCapability.reasoning?.default_effort === effort
									? ' (model default)'
									: ''}
							</option>
						{/each}
					</select>
				</label>
			{/if}

			{#if settings.reasoningMode === 'tokens'}
				<label>
					<span>Reasoning token budget</span>
					<input
						type="number"
						min="256"
						max="32000"
						bind:value={settings.reasoningMaxTokens}
						onchange={save}
					/>
				</label>
			{/if}

			{#if settings.reasoningMode !== 'off' && settings.modelCapability.supports_include_reasoning}
				<label class="checkline">
					<input
						type="checkbox"
						bind:checked={settings.excludeReasoningTrace}
						onchange={save}
					/>
					<span>
						Hide the reasoning trace in responses. Worth knowing: this only stops the text coming
						back, it does not stop the thinking — the reasoning tokens are still generated and
						still billed.
					</span>
				</label>
			{/if}

			<p class="hint">
				Research quality here comes from searching and reading, not from thinking harder in one
				turn, so Off is a reasonable default and keeps passes inside the time limit. Raise it when a
				subject is genuinely tangled.
			</p>
		{/if}

		<p class="hint">
			Sent to the endpoint as: <code>{JSON.stringify(reasoningPayload())}</code>
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
			<input type="number" min="30" max="240" bind:value={settings.budgetSeconds} onchange={save} />
		</label>
		<p class="hint">
			Vercel's Hobby plan kills any request at 300 seconds, so research runs in passes: the graph
			stops just short of the limit, hands back its state, and the next pass picks up where it left
			off. A cut-off round still spends one turn writing up what it found, which was measured
			overrunning by about 30 seconds — so 200 leaves headroom under the 300 second wall. Raise it
			only on Pro, where the ceiling is 800.
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
	.currentmodel {
		font-size: 12px;
		color: var(--muted);
		align-self: center;
		word-break: break-all;
	}
	.results {
		display: flex;
		flex-direction: column;
		border: 1.5px solid var(--line);
		border-radius: 13px;
		overflow: hidden;
		max-height: 320px;
		overflow-y: auto;
	}
	.modelrow {
		display: flex;
		flex-direction: column;
		gap: 3px;
		text-align: left;
		padding: 11px 14px;
		border-bottom: 1px solid var(--line);
	}
	.modelrow:last-child {
		border-bottom: none;
	}
	.modelrow.chosen {
		background: color-mix(in srgb, var(--ink) 7%, transparent);
	}
	.modelname {
		font-size: 15px;
	}
	.modelmeta {
		font-size: 11.5px;
		color: var(--muted);
		word-break: break-all;
	}
	.checkline {
		flex-direction: row;
		align-items: flex-start;
		gap: 10px;
	}
	.checkline input {
		width: auto;
		margin-top: 3px;
		flex: 0 0 auto;
	}
	code {
		font-size: 11.5px;
		word-break: break-all;
	}
</style>
