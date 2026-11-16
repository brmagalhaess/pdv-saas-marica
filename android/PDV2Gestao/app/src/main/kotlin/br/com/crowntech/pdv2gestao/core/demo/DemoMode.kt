package br.com.crowntech.pdv2gestao.core.demo

import br.com.crowntech.pdv2gestao.core.api.GestaoApi
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asStateFlow

/**
 * Chave entre a API real e a demonstração ("Ver demonstração" na tela Entrar).
 * Vale também no build de produção: as lojas (Apple/Google) revisam o app sem senha.
 * Entrar ou sair não grava nada: nem token, nem Keystore, nem preferências.
 */
class DemoMode(
    private val real: GestaoApi,
    /** Testes e capturas de tela trocam por um relógio fixo. */
    var demoFactory: () -> DemoApi = { DemoApi() },
) {
    private val _active = MutableStateFlow(false)
    val active: StateFlow<Boolean> = _active.asStateFlow()

    /** Criada a cada entrada e descartada na saída (nenhum dado sobra em memória). */
    private var demo: DemoApi? = null

    val api: GestaoApi get() = demo?.takeIf { _active.value } ?: real

    fun enter(): DemoApi {
        val fresh = demoFactory()
        demo = fresh
        _active.value = true
        return fresh
    }

    fun exit() {
        _active.value = false
        demo = null
    }
}
