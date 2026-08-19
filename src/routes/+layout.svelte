<script lang="ts">
	import '../app.css';
	import { page } from '$app/state';
	import VaultGate from '$lib/components/VaultGate.svelte';
	import { backToLibrary } from '$lib/library.svelte';

	let { children } = $props();
</script>

<VaultGate>
	<div class="shell">
		<header>
			<a class="wordmark" href="/" onclick={backToLibrary}>Praxis</a>
			<nav>
				<a href="/" class:on={page.url.pathname === '/'} onclick={backToLibrary}>Library</a>
				<a href="/settings" class:on={page.url.pathname === '/settings'}>Settings</a>
			</nav>
		</header>

		<main>
			{@render children()}
		</main>
	</div>
</VaultGate>

<style>
	.shell {
		max-width: var(--measure);
		margin: 0 auto;
		padding: calc(10px + var(--safe-top)) 16px 0;
	}
	header {
		display: flex;
		align-items: baseline;
		justify-content: space-between;
		padding: 6px 0 18px;
	}
	.wordmark {
		font-family: var(--serif);
		font-size: 21px;
		letter-spacing: -0.01em;
		text-decoration: none;
	}
	nav {
		display: flex;
		gap: 16px;
	}
	nav a {
		font-size: 13px;
		color: var(--muted);
		text-decoration: none;
	}
	nav a.on {
		color: var(--ink);
	}
</style>
