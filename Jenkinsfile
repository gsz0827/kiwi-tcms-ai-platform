pipeline {
    agent any
    options {
        disableConcurrentBuilds()
        timeout(time: 20, unit: 'MINUTES')
        buildDiscarder(logRotator(numToKeepStr: '20'))
        skipDefaultCheckout(true)
    }
    parameters {
        choice(name: 'TEST_TYPE', choices: ['api', 'web'], description: '自动化类型')
        string(name: 'SUITE_ID', defaultValue: '', description: '平台套件编号')
        string(name: 'TOKEN_CREDENTIAL_ID', defaultValue: '', description: '套件 CI 令牌的 Secret text 凭据 ID')
        string(name: 'KIWI_URL', defaultValue: 'https://localhost:8443', description: '平台 HTTPS 地址，须与证书匹配')
        string(name: 'CONNECT_HOST', defaultValue: 'kiwi-web', description: '本机 Docker 内部连接主机；远程 Jenkins 留空')
        choice(name: 'WEB_MODE', choices: ['debug', 'formal'], description: 'Web 执行方式')
        string(name: 'PLAN_ID', defaultValue: '', description: 'Web 正式执行的计划编号')
        string(name: 'BUILD_ID', defaultValue: '', description: 'Web 正式执行的构建编号')
        string(name: 'ENVIRONMENT_ID', defaultValue: '', description: 'Web 执行环境编号，空值使用套件默认环境')
    }
    stages {
        stage('准备本次报告') {
            steps {
                // Prevent a checkout/credential failure from publishing a previous build's report.
                writeFile file: 'kiwi-report.json', text: '{"status":"error","passed":false,"terminal":true,"error":"本次构建尚未取得测试结果","results":[]}'
                writeFile file: 'kiwi-report.xml', text: '<testsuite name="Kiwi CI" tests="1" errors="1"><testcase name="本次构建完整性"><error message="尚未取得测试结果"/></testcase></testsuite>'
            }
        }
        stage('检出平台客户端') {
            steps {
                checkout scm
            }
        }
        stage('执行测试套件') {
            steps {
                withCredentials([string(credentialsId: params.TOKEN_CREDENTIAL_ID, variable: 'KIWI_CI_TOKEN')]) {
                    withEnv(["KIWI_BASE_URL=${params.KIWI_URL}", "KIWI_CONNECT_HOST=${params.CONNECT_HOST}",
                             "CI_TEST_TYPE=${params.TEST_TYPE}", "CI_SUITE_ID=${params.SUITE_ID}",
                             "CI_WEB_MODE=${params.WEB_MODE}", "CI_PLAN_ID=${params.PLAN_ID}",
                             "CI_BUILD_ID=${params.BUILD_ID}", "CI_ENVIRONMENT_ID=${params.ENVIRONMENT_ID}"]) {
                        script {
                            int code = sh(returnStatus: true, script: '''
                                set +x
                                export KIWI_CA_BUNDLE="${KIWI_CA_BUNDLE:-/run/kiwi-secrets/kiwi-ca.pem}"
                                python3 deployment/jenkins/run_suite.py
                            ''')
                            if (code == 1) { unstable('测试失败或未完整执行，请查看 JUnit 和平台结果') }
                            if (code != 0 && code != 1) { error('CI 调用失败，请检查令牌、配置、证书及平台状态') }
                        }
                    }
                }
            }
        }
    }
    post {
        always {
            junit testResults: 'kiwi-report.xml', allowEmptyResults: false
            archiveArtifacts artifacts: 'kiwi-report.json,kiwi-report.xml', allowEmptyArchive: true
        }
    }
}
