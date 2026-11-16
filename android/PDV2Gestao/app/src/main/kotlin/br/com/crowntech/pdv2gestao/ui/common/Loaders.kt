package br.com.crowntech.pdv2gestao.ui.common

import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.Stable
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateListOf
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.rememberCoroutineScope
import androidx.compose.runtime.rememberUpdatedState
import androidx.compose.runtime.setValue
import br.com.crowntech.pdv2gestao.core.api.ApiException
import kotlinx.coroutines.CancellationException
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Job
import kotlinx.coroutines.launch
import kotlinx.datetime.Clock
import kotlinx.datetime.Instant

private fun Throwable.asApi(): ApiException = this as? ApiException ?: ApiException.server(0)

/** Estado de uma tela que carrega um objeto só (Resumo, detalhes, terminais). */
@Stable
class LoadState<T>(
    private val scope: CoroutineScope,
    private val fetch: suspend () -> T,
    private val onError: (ApiException) -> Unit,
) {
    var data by mutableStateOf<T?>(null)
        private set
    var error by mutableStateOf<ApiException?>(null)
        private set
    var loading by mutableStateOf(false)
        private set
    var refreshing by mutableStateOf(false)
        private set
    var updatedAt by mutableStateOf<Instant?>(null)
        private set
    /** Falhou ao atualizar, mas há dados antigos na tela. */
    var staleError by mutableStateOf<ApiException?>(null)
        private set

    private var job: Job? = null

    fun reload(userInitiated: Boolean = false) {
        job?.cancel()
        job = scope.launch {
            if (userInitiated) refreshing = true else loading = data == null
            try {
                val result = fetch()
                data = result
                error = null
                staleError = null
                updatedAt = Clock.System.now()
            } catch (e: CancellationException) {
                throw e
            } catch (e: Throwable) {
                val api = e.asApi()
                if (data != null) staleError = api else error = api
                onError(api)
            } finally {
                loading = false
                refreshing = false
            }
        }
    }
}

@Composable
fun <T> rememberLoad(vararg keys: Any?, onError: (ApiException) -> Unit, fetch: suspend () -> T): LoadState<T> {
    val scope = rememberCoroutineScope()
    val latestFetch by rememberUpdatedState(fetch)
    val latestOnError by rememberUpdatedState(onError)
    val state = remember(*keys) { LoadState(scope, { latestFetch() }, { latestOnError(it) }) }
    LaunchedEffect(state) { state.reload() }
    return state
}

/** Uma página vinda da API, com cabeçalho opcional (totais/contagens). */
data class PageResult<H, T>(val header: H?, val items: List<T>, val nextCursor: String?)

/**
 * Junta a página nova à lista sem repetir itens. O servidor pagina por offset: se entram
 * registros novos entre uma página e outra, a página seguinte devolve itens que já estão
 * na tela. Repetidos quebram o LazyColumn (chave usada duas vezes) e confundem o usuário.
 */
internal fun <T> newItemsOnly(existing: List<T>, page: List<T>, keyOf: (T) -> Any): List<T> {
    val seen = existing.mapTo(HashSet()) { keyOf(it) }
    return page.filter { seen.add(keyOf(it)) }
}

/** Lista com rolagem infinita por next_cursor. [keyOf] identifica cada item (o mesmo usado como key no LazyColumn). */
@Stable
class PagedState<H, T>(
    private val scope: CoroutineScope,
    private val fetch: suspend (cursor: String?) -> PageResult<H, T>,
    private val onError: (ApiException) -> Unit,
    private val keyOf: (T) -> Any,
) {
    val items = mutableStateListOf<T>()
    var header by mutableStateOf<H?>(null)
        private set
    var nextCursor by mutableStateOf<String?>(null)
        private set
    var loaded by mutableStateOf(false)
        private set
    var loading by mutableStateOf(false)
        private set
    var refreshing by mutableStateOf(false)
        private set
    var loadingMore by mutableStateOf(false)
        private set
    var error by mutableStateOf<ApiException?>(null)
        private set
    var moreError by mutableStateOf<ApiException?>(null)
        private set
    var staleError by mutableStateOf<ApiException?>(null)
        private set
    var updatedAt by mutableStateOf<Instant?>(null)
        private set

    private var job: Job? = null

    fun reload(userInitiated: Boolean = false) {
        job?.cancel()
        job = scope.launch {
            if (userInitiated) refreshing = true else loading = !loaded
            try {
                val page = fetch(null)
                items.clear()
                items.addAll(newItemsOnly(emptyList(), page.items, keyOf))
                header = page.header
                nextCursor = page.nextCursor
                loaded = true
                error = null
                staleError = null
                moreError = null
                updatedAt = Clock.System.now()
            } catch (e: CancellationException) {
                throw e
            } catch (e: Throwable) {
                val api = e.asApi()
                if (loaded) staleError = api else error = api
                onError(api)
            } finally {
                loading = false
                refreshing = false
            }
        }
    }

    fun loadMore() {
        val cursor = nextCursor ?: return
        if (loadingMore || loading || refreshing) return
        loadingMore = true
        scope.launch {
            try {
                val page = fetch(cursor)
                items.addAll(newItemsOnly(items, page.items, keyOf))
                if (page.header != null) header = page.header
                nextCursor = page.nextCursor
                moreError = null
            } catch (e: CancellationException) {
                throw e
            } catch (e: Throwable) {
                moreError = e.asApi()
            } finally {
                loadingMore = false
            }
        }
    }
}

@Composable
fun <H, T> rememberPaged(
    vararg keys: Any?,
    keyOf: (T) -> Any,
    onError: (ApiException) -> Unit,
    fetch: suspend (cursor: String?) -> PageResult<H, T>,
): PagedState<H, T> {
    val scope = rememberCoroutineScope()
    val latestFetch by rememberUpdatedState(fetch)
    val latestOnError by rememberUpdatedState(onError)
    val latestKeyOf by rememberUpdatedState(keyOf)
    val state = remember(*keys) { PagedState<H, T>(scope, { latestFetch(it) }, { latestOnError(it) }, { latestKeyOf(it) }) }
    LaunchedEffect(state) { state.reload() }
    return state
}
