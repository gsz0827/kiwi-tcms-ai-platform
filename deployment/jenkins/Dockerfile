FROM jenkins/jenkins:lts-jdk21
USER root
RUN apt-get update && apt-get install -y --no-install-recommends python3 ca-certificates && apt-get clean
USER jenkins
RUN jenkins-plugin-cli --plugins workflow-aggregator credentials-binding junit git
ENV JAVA_OPTS="-Djenkins.install.runSetupWizard=false -Xms128m -Xmx512m"
