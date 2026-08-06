package com.rsdynamics.app

import android.annotation.SuppressLint
import android.graphics.Color
import android.os.Bundle
import android.webkit.JavascriptInterface
import android.webkit.WebView
import android.webkit.WebViewClient
import androidx.activity.ComponentActivity
import androidx.activity.OnBackPressedCallback
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.launch
import kotlinx.coroutines.withContext
import androidx.lifecycle.lifecycleScope
import org.json.JSONObject
import java.net.URL

/**
 * v0 셸 — 화면을 눈으로 확정하기 위한 단계다.
 *
 * 지금은 세 화면 전부를 WebView 한 장으로 띄운다. 화면 구성이 확정되면
 * '오늘'과 '종목 상세'만 Compose로 옮기고, 4분면은 이 WebView를 그대로 남긴다.
 * (자산 파일 app.html 이 그때 quadrant 전용으로 줄어든다.)
 *
 * 스냅샷 우선순위: 원격 SNAPSHOT_URL → 실패 시 assets 에 내장된 데모 데이터
 */
class MainActivity : ComponentActivity() {

    /** Colab 이 업로드하는 latest.json 의 공개 URL. 비워두면 내장 데모로 뜬다. */
    private val snapshotUrl: String? = null
    // 예: "https://firebasestorage.googleapis.com/.../latest.json"

    private lateinit var web: WebView

    @SuppressLint("SetJavaScriptEnabled")
    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)

        web = WebView(this).apply {
            setBackgroundColor(Color.parseColor("#0B111B"))
            settings.javaScriptEnabled = true
            settings.domStorageEnabled = true
            // 확대 제스처는 4분면이 직접 처리한다. 브라우저 줌이 끼면 두 겹이 된다.
            settings.builtInZoomControls = false
            settings.textZoom = 100
            addJavascriptInterface(Bridge(), "AppBridge")
            webViewClient = object : WebViewClient() {
                override fun onPageFinished(view: WebView?, url: String?) {
                    snapshotUrl?.let { loadRemoteSnapshot(it) }
                }
            }
        }
        setContentView(web)
        web.loadUrl("file:///android_asset/app.html")

        // 뒤로가기: 상세가 열려 있으면 상세만 닫는다.
        onBackPressedDispatcher.addCallback(this, object : OnBackPressedCallback(true) {
            override fun handleOnBackPressed() {
                web.evaluateJavascript(
                    "(function(){var d=document.getElementById('detail');" +
                    "if(d&&d.classList.contains('on')){d.classList.remove('on');return 'closed';}" +
                    "return 'exit';})()"
                ) { result ->
                    if (result.trim('"') == "exit") finish()
                }
            }
        })
    }

    /** 원격 스냅샷을 받아 JS 로 밀어 넣는다. 실패하면 화면은 내장 데모를 유지한다. */
    private fun loadRemoteSnapshot(url: String) {
        lifecycleScope.launch {
            val json = withContext(Dispatchers.IO) {
                runCatching { URL(url).readText() }.getOrNull()
            } ?: return@launch
            // JSON 을 JS 문자열 리터럴로 안전하게 감싼다
            val quoted = JSONObject.quote(json)
            web.evaluateJavascript("window.quadrant.loadSnapshot($quoted)", null)
        }
    }

    inner class Bridge {
        /** 4분면에서 종목을 선택했을 때 호출된다. Compose 전환 후 상세 화면 진입점이 된다. */
        @JavascriptInterface
        fun onSelect(ticker: String) {
            runOnUiThread {
                web.evaluateJavascript("openDetail('$ticker')", null)
            }
        }
    }
}
