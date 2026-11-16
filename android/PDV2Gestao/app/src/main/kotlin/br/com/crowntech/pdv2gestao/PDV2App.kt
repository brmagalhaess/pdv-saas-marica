package br.com.crowntech.pdv2gestao

import android.app.Application
import br.com.crowntech.pdv2gestao.core.api.ApiClient
import br.com.crowntech.pdv2gestao.core.api.AppApi
import br.com.crowntech.pdv2gestao.core.api.AppConfig
import br.com.crowntech.pdv2gestao.core.demo.DemoMode
import br.com.crowntech.pdv2gestao.core.security.AppPrefs
import br.com.crowntech.pdv2gestao.core.security.SecureSessionStore
import br.com.crowntech.pdv2gestao.core.session.SessionManager

/** Dependências do app (sem framework de injeção). */
class AppContainer(app: Application) {
    val client = ApiClient(userAgent = AppConfig.userAgent())
    /** Testes trocam por uma versão que conta gravações. */
    var prefs: AppPrefs = AppPrefs(app)
        internal set
    val session = SessionManager(client, SecureSessionStore(app))
    val api = AppApi(client, session)
    /** "Ver demonstração": troca a API real por dados fictícios, sem rede. */
    val demo = DemoMode(api)
}

class PDV2App : Application() {
    lateinit var container: AppContainer
        private set

    override fun onCreate() {
        super.onCreate()
        container = AppContainer(this)
    }
}
