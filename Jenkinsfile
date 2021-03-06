#!/usr/bin/env groovy

pipeline {
    agent { docker defaultWorker.getConfig("openstack-test") }
    options {
        ansiColor('xterm')
        timestamps()
        timeout(time: 2, unit: "HOURS")
    }
    stages {
        stage("systest") {
            steps {
                sh '''
                    # - source this job's environment variables
                    export ENV_FILE=systest/${JOB_BASE_NAME}.env
                    if [ -e $ENV_FILE ]; then
                        . $ENV_FILE
                    fi

                    # - print build properties
                    printenv | sort | grep -v OS_PASSWORD

                    # - setup ssh agent
                    eval $(ssh-agent -s)
                    ssh-add

                    # - run tests
                    make -C systest $JOB_BASE_NAME

                    # - copy results files to nfs
                    #   (note that the nfs results directory is mounted inside
                    #   the CI worker's home directory)
                    cp -r $WORKSPACE/systest/test_results/* ~/results/
                '''
            }
        }
    }
    post {
        always {
            // cleanup workspace
            dir("${env.WORKSPACE}") { deleteDir() }
        }
    }
}
