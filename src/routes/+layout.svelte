<script lang="ts">
	import '../app.css';
	import { page } from '$app/state';

	let { children } = $props();
</script>

<div class="shell">
	<header>
		<a class="wordmark" href="/">Praxis</a>
		<nav>
			<a href="/" class:on={page.url.pathname === '/'}>Lesson</a>
			<a href="/settings" class:on={page.url.pathname === '/settings'}>Settings</a>
		</nav>
	</header>

	<main>
		{@render children()}
	</main>
</div>

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
