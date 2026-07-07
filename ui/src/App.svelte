<script lang="ts">
  import { QueryClient, QueryClientProvider } from "@tanstack/svelte-query";
  import Shell from "./Shell.svelte";

  // App only provides the QueryClient. The shell and everything that consumes a
  // query lives in Shell, INSIDE the provider - so a query used for the shell
  // itself (e.g. admin-nav gating) has a client in context. A query created in
  // this component's script would run outside the provider and fail.
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: 1, staleTime: 15_000 } },
  });
</script>

<QueryClientProvider client={queryClient}>
  <Shell />
</QueryClientProvider>
