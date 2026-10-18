pipeline {
    agent any
    options {
        disableConcurrentBuilds()
        skipStagesAfterUnstable()
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
        choice(name: 'WEB_MODE', choices: ['debug', 'formal'], description: 'Web/API 共用执行方式（沿用旧参数名）')
        string(name: 'PLAN_ID', defaultValue: '', description: '正式执行的计划编号')
        string(name: 'BUILD_ID', defaultValue: '', description: '正式执行的构建编号')
        string(name: 'ENVIRONMENT_ID', defaultValue: '', description: '执行环境编号，空值使用套件默认环境')
        booleanParam(name: 'RELEASE_CHECK', defaultValue: false, description: '正式执行后等待人工归档和审批，检查门禁并模拟发布；不执行真实部署')
    }
    environment {
        KIWI_BASE_URL = "${params.KIWI_URL}"
        KIWI_CONNECT_HOST = "${params.CONNECT_HOST}"
        CI_TEST_TYPE = "${params.TEST_TYPE}"
        CI_SUITE_ID = "${params.SUITE_ID}"
        CI_WEB_MODE = "${params.WEB_MODE}"
        CI_PLAN_ID = "${params.PLAN_ID}"
        CI_BUILD_ID = "${params.BUILD_ID}"
        CI_ENVIRONMENT_ID = "${params.ENVIRONMENT_ID}"
    }
    stages {
        stage('准备本次报告') {
            steps {
                writeFile file: 'kiwi-report.json', text: '{"status":"error","passed":false,"terminal":true,"error":"本次构建尚未取得测试结果","results":[]}'
                writeFile file: 'kiwi-report.xml', text: '<testsuite name="Kiwi CI" tests="1" errors="1"><testcase name="本次构建完整性"><error message="尚未取得测试结果"/></testcase></testsuite>'
                writeFile file: 'kiwi-release-gate.json', text: '{"allowed":false,"reason":"not_checked"}'
                writeFile file: 'kiwi-release-simulation.json', text: '{"allowed":false,"deployment_performed":false,"reason":"not_executed"}'
            }
        }
        stage('校验发布检查参数') {
            when { expression { params.RELEASE_CHECK } }
            steps {
                script {
                    if (params.WEB_MODE != 'formal' || !params.PLAN_ID?.trim() || !params.BUILD_ID?.trim()) {
                        error('发布检查必须使用正式执行，并填写计划和构建；调试结果不能用于发布')
                    }
                }
            }
        }
        stage('检出平台客户端') {
            steps { checkout scm }
        }
        stage('执行测试套件') {
            steps {
                withCredentials([string(credentialsId: params.TOKEN_CREDENTIAL_ID, variable: 'KIWI_CI_TOKEN')]) {
                    script {
                        int code = sh(returnStatus: true, script: '''
                            set +x
                            export KIWI_CA_BUNDLE="${KIWI_CA_BUNDLE:-/run/kiwi-secrets/kiwi-ca.pem}"
                            python3 deployment/jenkins/run_suite.py
                        ''')
                        if (code == 1) { unstable('测试失败或未完整执行，禁止进入发布阶段') }
                        if (code != 0 && code != 1) { error('CI 调用失败，请检查令牌、配置、证书及平台状态') }
                    }
                }
            }
        }
        stage('发布准入检查') {
            when { expression { params.RELEASE_CHECK } }
            steps {
                echo '等待本次正式执行的结果归档及报告审批（最多 10 分钟）；CI 不代替人工审批。'
                withCredentials([string(credentialsId: params.TOKEN_CREDENTIAL_ID, variable: 'KIWI_CI_TOKEN')]) {
                    script {
                        int code = sh(returnStatus: true, script: '''
                            set +x
                            export KIWI_CA_BUNDLE="${KIWI_CA_BUNDLE:-/run/kiwi-secrets/kiwi-ca.pem}"
                            python3 deployment/jenkins/run_suite.py gate
                        ''')
                        if (code != 0) { error('发布门禁未通过或检查异常，模拟发布已阻断；请查看门禁 JSON') }
                    }
                }
            }
        }
        stage('模拟发布（不执行部署）') {
            when { expression { params.RELEASE_CHECK } }
            steps {
                // Recheck at the boundary: an earlier green result is not a reusable permit.
                withCredentials([string(credentialsId: params.TOKEN_CREDENTIAL_ID, variable: 'KIWI_CI_TOKEN')]) {
                    script {
                        int code = sh(returnStatus: true, script: '''
                            set +x
                            export KIWI_CA_BUNDLE="${KIWI_CA_BUNDLE:-/run/kiwi-secrets/kiwi-ca.pem}"
                            python3 deployment/jenkins/run_suite.py simulate
                        ''')
                        if (code != 0) { error('模拟发布前复核未通过，已停止；没有执行真实部署') }
                    }
                }
                echo '本次构建已满足发布检查；仅生成模拟记录，没有执行真实部署。'
            }
        }
    }
    post {
        always {
            junit testResults: 'kiwi-report.xml', allowEmptyResults: false
            archiveArtifacts artifacts: 'kiwi-report.json,kiwi-report.xml,kiwi-release-gate.json,kiwi-release-simulation.json', allowEmptyArchive: true
        }
    }
}
