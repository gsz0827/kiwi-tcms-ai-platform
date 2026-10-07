import jenkins.model.Jenkins
import hudson.security.HudsonPrivateSecurityRealm
import hudson.security.FullControlOnceLoggedInAuthorizationStrategy
import groovy.json.JsonSlurper
import com.cloudbees.plugins.credentials.CredentialsScope
import com.cloudbees.plugins.credentials.SystemCredentialsProvider
import org.jenkinsci.plugins.plaincredentials.impl.StringCredentialsImpl
import hudson.util.Secret
import hudson.model.ParametersDefinitionProperty
import hudson.model.StringParameterDefinition
import hudson.model.ChoiceParameterDefinition
import org.jenkinsci.plugins.workflow.job.WorkflowJob
import org.jenkinsci.plugins.workflow.cps.CpsScmFlowDefinition
import hudson.plugins.git.GitSCM
import hudson.plugins.git.UserRemoteConfig
import hudson.plugins.git.BranchSpec

def jenkins = Jenkins.get()
def marker = new File(jenkins.rootDir, 'kiwi-bootstrap-complete')
if (!marker.exists()) {
    // Fail closed if initialization data is absent; never start an anonymous controller.
    def realm = new HudsonPrivateSecurityRealm(false)
    jenkins.setSecurityRealm(realm)
    def strategy = new FullControlOnceLoggedInAuthorizationStrategy()
    strategy.setAllowAnonymousRead(false)
    jenkins.setAuthorizationStrategy(strategy)
    jenkins.save()
    def bootstrap = new JsonSlurper().parse(new File('/run/kiwi-secrets/bootstrap.json'))
    realm.createAccount(bootstrap.username, bootstrap.password)
    jenkins.setNumExecutors(1)
    def credentials = SystemCredentialsProvider.getInstance()
    bootstrap.credentials.each { entry ->
        credentials.getCredentials().add(new StringCredentialsImpl(CredentialsScope.GLOBAL,
            entry.id, entry.description, Secret.fromString(entry.token)))
    }
    credentials.save()
    def job = jenkins.createProject(WorkflowJob, 'Kiwi-Automation')
    job.setDescription('Kiwi 套件执行与 JUnit 报告。参数中选择接口或 Web 套件；令牌由 Jenkins 凭据管理。')
    def scm = new GitSCM([new UserRemoteConfig('https://github.com/gsz0827/kiwi-tcms-ai-platform.git', null, null, null)],
                        [new BranchSpec('*/main')], false, [], null, null, [])
    def definition = new CpsScmFlowDefinition(scm, 'Jenkinsfile')
    definition.setLightweight(true)
    job.setDefinition(definition)
    job.addProperty(new ParametersDefinitionProperty([
        new ChoiceParameterDefinition('TEST_TYPE', ['api','web'] as String[], '自动化类型'),
        new StringParameterDefinition('SUITE_ID', (bootstrap.suites?.api_pass ?: '').toString()),
        new StringParameterDefinition('TOKEN_CREDENTIAL_ID', bootstrap.default_credential_id ?: (bootstrap.credentials ? bootstrap.credentials[0].id : '')),
        new StringParameterDefinition('KIWI_URL', 'https://localhost:8443'),
        new StringParameterDefinition('CONNECT_HOST', 'kiwi-web'),
        new ChoiceParameterDefinition('WEB_MODE', ['debug','formal'] as String[], 'Web 执行方式'),
        new StringParameterDefinition('PLAN_ID',''),
        new StringParameterDefinition('BUILD_ID',''),
        new StringParameterDefinition('ENVIRONMENT_ID','')
    ]))
    job.save()
    jenkins.save()
    marker.createNewFile()
    println('Kiwi Jenkins initialized with authentication and a scoped test pipeline; secrets are not logged.')
}
